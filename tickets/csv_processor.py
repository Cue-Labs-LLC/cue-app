import csv
import json
import logging
from decimal import Decimal
from datetime import datetime, timezone as dt_timezone
from typing import Dict, List, Optional, Tuple
from django.db import transaction
from django.utils import timezone
from dateutil import parser as date_parser
import pandas as pd
import os

from .models import (
    CSVFormat, UploadedFile, Customer, Event, TicketOrder, Ticket, TicketTier, Venue
)

logger = logging.getLogger(__name__)


class CSVProcessor:
    """Service class for processing CSV files with format-aware column mapping."""
    
    CHUNK_SIZE = 500  # Process 500 rows at a time
    
    def __init__(self, uploaded_file: UploadedFile, csv_format: CSVFormat):
        self.uploaded_file = uploaded_file
        self.csv_format = csv_format
        self.column_mapping = csv_format.column_mapping
        self.requires_manual_pricing = csv_format.requires_manual_pricing
        self.uses_tiers = csv_format.uses_tiers
        
    def validate_csv(self, file) -> Tuple[bool, Optional[str]]:
        """Check file format and structure."""
        try:
            # Check file extension
            if not file.name.lower().endswith('.csv'):
                return False, "File must be a CSV file."
            
            # Try to read first few rows to validate structure
            file.seek(0)
            sample = file.read(1024)
            file.seek(0)
            
            # Check if it's readable as CSV
            try:
                csv.Sniffer().sniff(sample.decode('utf-8'))
            except (csv.Error, UnicodeDecodeError):
                return False, "Invalid CSV format or encoding."
            
            return True, None
        except Exception as e:
            logger.error(f"CSV validation error: {str(e)}")
            return False, f"Error validating CSV: {str(e)}"
    
    def map_columns(self, row: Dict) -> Dict:
        """Map CSV column names to internal field names using format configuration."""
        mapped = {}
        
        for internal_field, csv_columns in self.column_mapping.items():
            # Special handling for customer_name - combine multiple columns
            if internal_field == 'customer_name' and len(csv_columns) > 1:
                # Combine First Name and Last Name
                name_parts = []
                for csv_col in csv_columns:
                    value = None
                    # Try exact match first
                    if csv_col in row:
                        value = str(row[csv_col]).strip() if row[csv_col] else ''
                    else:
                        # Try case-insensitive match
                        for key in row.keys():
                            if key.lower() == csv_col.lower():
                                value = str(row[key]).strip() if row[key] else ''
                                break
                    if value:
                        name_parts.append(value)
                mapped[internal_field] = ' '.join(name_parts) if name_parts else None
            elif internal_field == 'processed_in_person':
                # Optional: map CSV column(s) and normalize to boolean
                value = None
                for csv_col in csv_columns:
                    if csv_col in row:
                        value = row[csv_col]
                        break
                    for key in row.keys():
                        if key.lower() == csv_col.lower():
                            value = row[key]
                            break
                    if value is not None:
                        break
                if value is not None:
                    s = str(value).strip().lower()
                    mapped[internal_field] = s in ('true', 'yes', '1')
                else:
                    mapped[internal_field] = False
            else:
                # Standard single column mapping
                value = None
                for csv_col in csv_columns:
                    # Try exact match first
                    if csv_col in row:
                        value = row[csv_col]
                        break
                    # Try case-insensitive match
                    for key in row.keys():
                        if key.lower() == csv_col.lower():
                            value = row[key]
                            break
                    if value is not None:
                        break
                
                mapped[internal_field] = value
        
        return mapped
    
    def parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse date string to datetime object with timezone awareness."""
        if not date_str:
            return None
        try:
            parsed = date_parser.parse(str(date_str))
            # Make timezone-aware if it's naive (Django requires timezone-aware when USE_TZ=True)
            if parsed and timezone.is_naive(parsed):
                # Assume UTC if no timezone info is provided
                # Use Python's built-in UTC timezone (works with all Django versions)
                parsed = timezone.make_aware(parsed, dt_timezone.utc)
            return parsed
        except (ValueError, TypeError):
            logger.warning(f"Could not parse date: {date_str}")
            return None
    
    def parse_csv(self, file) -> List[Dict]:
        """Read and parse CSV using column mappings."""
        file.seek(0)
        rows = []
        
        try:
            # Use pandas for efficient CSV reading with chunking support
            chunk_iter = pd.read_csv(
                file,
                chunksize=self.CHUNK_SIZE,
                dtype=str,  # Read all as strings to preserve data
                keep_default_na=False  # Don't convert empty strings to NaN
            )
            
            for chunk in chunk_iter:
                # Convert DataFrame to list of dicts
                chunk_rows = chunk.to_dict('records')
                rows.extend(chunk_rows)
        except Exception as e:
            logger.error(f"Error parsing CSV: {str(e)}")
            raise
        
        return rows
    
    # Reason codes for skipped rows (used in processing_results and results template)
    SKIP_REASON_TOO_FEW_COLUMNS = "too_few_columns"
    SKIP_REASON_HEADER_REPEAT = "header_repeat"

    def _should_skip_row(self, row: Dict) -> Tuple[bool, Optional[str]]:
        """
        Determine if a row should be skipped (not real data). Returns (skip, reason_code).
        Reason codes are used on the Processing Results page to explain why rows were skipped.
        """
        if not row:
            return True, self.SKIP_REASON_TOO_FEW_COLUMNS
        non_empty = sum(1 for v in row.values() if v is not None and str(v).strip())
        min_required_cells = 4
        if non_empty < min_required_cells:
            return True, self.SKIP_REASON_TOO_FEW_COLUMNS
        # Skip if any mapped column value equals the column header (repeated header row)
        for internal_field, csv_columns in self.column_mapping.items():
            for csv_col in csv_columns:
                val = None
                if csv_col in row:
                    val = row[csv_col]
                else:
                    for k in row.keys():
                        if str(k).lower() == str(csv_col).lower():
                            val = row[k]
                            break
                if val is not None and str(val).strip():
                    if str(val).strip().lower() == str(csv_col).strip().lower():
                        return True, self.SKIP_REASON_HEADER_REPEAT
        return False, None

    def validate_ticket_order_data(self, row: Dict) -> Tuple[bool, Optional[str]]:
        """Validate individual ticket order row."""
        # In-person rows (processed_in_person=true) only require order_date and ticket_type
        if row.get('processed_in_person'):
            required_fields = ['order_date', 'ticket_type']
        else:
            required_fields = ['order_date', 'customer_email', 'customer_name', 'ticket_type']
        
        missing = [field for field in required_fields if not row.get(field)]
        if missing:
            return False, f"Missing required fields: {', '.join(missing)}"
        
        # Validate order_date
        if not self.parse_date(row.get('order_date')):
            return False, f"Invalid order_date: {row.get('order_date')}"
        
        # Event fields are optional - will use metadata if not in CSV
        # Validate event_date only if provided
        if row.get('event_date') and not self.parse_date(row.get('event_date')):
            return False, f"Invalid event_date: {row.get('event_date')}"
        
        return True, None
    
    def generate_order_number(self, mapped_row: Dict) -> str:
        """Generate a unique order number from available data if not provided."""
        # If order_number is already provided, use it
        if mapped_row.get('order_number'):
            return mapped_row.get('order_number')
        
        # Generate from email + order_date
        email = mapped_row.get('customer_email', '').lower().strip()
        order_date = self.parse_date(mapped_row.get('order_date'))
        
        if email and order_date:
            # Create a unique identifier: email + timestamp
            timestamp_str = order_date.strftime('%Y%m%d-%H%M%S')
            # Remove special characters from email for filename safety
            email_clean = email.replace('@', '_at_').replace('.', '_')
            return f"ORD-{email_clean}-{timestamp_str}"
        
        # Fallback: use timestamp + random component
        import uuid
        return f"ORD-{timezone.now().strftime('%Y%m%d-%H%M%S')}-{str(uuid.uuid4())[:8]}"
    
    def process_and_save(
        self, 
        csv_data: List[Dict], 
        manual_prices: Optional[Dict[str, Decimal]] = None,
        tier_definitions: Optional[Dict] = None
    ) -> Dict:
        """
        Main processing logic with chunked processing and bulk operations.
        
        Returns:
            Dict with keys: success_count, error_count, skipped_duplicates, errors, rejected_orders
        """
        results = {
            'success_count': 0,
            'error_count': 0,
            'skipped_duplicates': 0,
            'errors': [],
            'skipped_order_numbers': [],
            'rejected_orders': [],
            'skipped_rows_count': 0,
            'skipped_rows_by_reason': {},
        }
        
        # If using tiers, create tier instances first
        if self.uses_tiers and tier_definitions:
            self._create_tier_instances(tier_definitions)
        
        total_rows = len(csv_data)
        self.uploaded_file.total_rows = total_rows
        self.uploaded_file.status = 'processing'
        self.uploaded_file.save(update_fields=['total_rows', 'status'])
        
        # Process in chunks
        for chunk_start in range(0, total_rows, self.CHUNK_SIZE):
            chunk_end = min(chunk_start + self.CHUNK_SIZE, total_rows)
            chunk_data = csv_data[chunk_start:chunk_end]
            
            try:
                with transaction.atomic():
                    if self.uses_tiers:
                        chunk_results = self._process_chunk(chunk_data, tier_definitions=tier_definitions)
                    else:
                        chunk_results = self._process_chunk(chunk_data, manual_prices=manual_prices)
                    
                    # Update results
                    results['success_count'] += chunk_results['success_count']
                    results['error_count'] += chunk_results['error_count']
                    results['skipped_duplicates'] += chunk_results['skipped_duplicates']
                    results['errors'].extend(chunk_results['errors'])
                    results['skipped_order_numbers'].extend(chunk_results['skipped_order_numbers'])
                    results['rejected_orders'].extend(chunk_results.get('rejected_orders', []))
                    results['skipped_rows_count'] += chunk_results.get('skipped_rows_count', 0)
                    for reason, count in chunk_results.get('skipped_rows_by_reason', {}).items():
                        results['skipped_rows_by_reason'][reason] = (
                            results['skipped_rows_by_reason'].get(reason, 0) + count
                        )
                    
                    # Update progress
                    self.uploaded_file.processed_rows = chunk_end
                    self.uploaded_file.save(update_fields=['processed_rows'])
                    
                    # Update customer LTV after each chunk
                    self._update_customer_ltv(chunk_results['customer_ids'])
                    
            except Exception as e:
                logger.error(f"Error processing chunk {chunk_start}-{chunk_end}: {str(e)}")
                results['error_count'] += len(chunk_data)
                results['errors'].append(f"Chunk {chunk_start}-{chunk_end}: {str(e)}")
        
        # Update final status
        if results['error_count'] == 0 and results['skipped_duplicates'] == 0:
            self.uploaded_file.status = 'completed'
        elif results['success_count'] > 0:
            self.uploaded_file.status = 'completed'  # Partial success
        else:
            self.uploaded_file.status = 'failed'
        
        self.uploaded_file.save(update_fields=['status'])
        
        return results
    
    def _create_tier_instances(self, tier_definitions: Dict):
        """Create TicketTier instances from tier definitions."""
        tiers_to_create = []
        
        for ticket_type, tiers in tier_definitions.items():
            for tier_data in tiers:
                tier = TicketTier(
                    ticket_type=ticket_type,
                    name=tier_data['name'],
                    price=Decimal(str(tier_data['price'])),
                    allotment=int(tier_data['allotment']),
                    order=int(tier_data['order']),
                    uploaded_file=self.uploaded_file,
                    tickets_assigned=0
                )
                tiers_to_create.append(tier)
        
        if tiers_to_create:
            TicketTier.objects.bulk_create(tiers_to_create)
    
    def _get_tiers_for_ticket_type(self, ticket_type: str) -> List[TicketTier]:
        """Get available tiers for a ticket type, sorted by order."""
        return list(
            TicketTier.objects.filter(
                uploaded_file=self.uploaded_file,
                ticket_type=ticket_type
            ).order_by('order')
        )
    
    def _assign_tier_to_ticket(self, ticket_type: str, quantity: int) -> Tuple[Optional[TicketTier], int]:
        """
        Assign a tier to tickets based on availability.
        Assigns all tickets in an order to the first available tier that has enough capacity.
        Returns (tier, quantity) or (None, 0) if no tier available.
        Note: All tickets in an order will use the same tier.
        """
        # Use select_for_update to lock tiers during assignment
        tiers = TicketTier.objects.filter(
            uploaded_file=self.uploaded_file,
            ticket_type=ticket_type
        ).select_for_update().order_by('order')
        
        if not tiers.exists():
            return None, 0
        
        # Find first tier with enough capacity for the entire order
        for tier in tiers:
            if tier.is_available():
                available = tier.remaining_capacity()
                if available >= quantity:
                    # This tier has enough capacity for the entire order
                    tier.tickets_assigned += quantity
                    tier.save(update_fields=['tickets_assigned'])
                    return tier, quantity
        
        # No tier has enough capacity - reject the order
        return None, 0
    
    def _process_chunk(
        self, 
        chunk_data: List[Dict], 
        manual_prices: Optional[Dict[str, Decimal]] = None,
        tier_definitions: Optional[Dict] = None
    ) -> Dict:
        """Process a single chunk of CSV rows."""
        # Classify each row: skip with reason or keep for processing
        skipped_by_reason = {}
        rows_to_process = []
        for row in chunk_data:
            skip, reason = self._should_skip_row(row)
            if skip and reason:
                skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
            else:
                rows_to_process.append(row)
        chunk_data = rows_to_process

        results = {
            'success_count': 0,
            'error_count': 0,
            'skipped_duplicates': 0,
            'errors': [],
            'skipped_order_numbers': [],
            'customer_ids': set(),
            'rejected_orders': [],
            'skipped_rows_count': sum(skipped_by_reason.values()),
            'skipped_rows_by_reason': skipped_by_reason,
        }
        
        customers_to_create = []
        customers_to_update = []
        events_to_create = []
        events_to_update = []
        ticket_orders_to_create = []
        tickets_to_create = []
        
        # Track existing customers and events for bulk operations (org-scoped)
        org = getattr(self.uploaded_file, 'organization', None)
        chunk_emails = [row.get('customer_email', '').lower().strip() for row in chunk_data if row.get('customer_email')]
        customer_filter = Customer.objects.filter(email__in=chunk_emails)
        if org is not None:
            customer_filter = customer_filter.filter(organization=org)
        existing_customers = {c.email: c for c in customer_filter}

        # Placeholder customer for in-person (no-email) rows; get-or-create when format supports it
        placeholder_customer = None
        if org is not None and 'processed_in_person' in self.column_mapping:
            placeholder_email = f"in-person-{org.id}@placeholder.local"
            placeholder_customer, _ = Customer.objects.get_or_create(
                organization=org,
                email=placeholder_email,
                defaults={'name': 'In-Person Sales'},
            )
            existing_customers[placeholder_email] = placeholder_customer

        existing_events = {}

        # Fast path: if event_id is in metadata, use that event directly (org-scoped)
        metadata_event_id = self.uploaded_file.metadata.get('event_id')
        pinned_event = None
        if metadata_event_id and org is not None:
            try:
                pinned_event = Event.objects.filter(organization=org).get(id=metadata_event_id)
                existing_events[(pinned_event.name, pinned_event.start_date)] = pinned_event
            except Event.DoesNotExist:
                pass

        # Pre-fetch existing events for this chunk
        # Get event info from metadata (used when CSV doesn't have event columns)
        metadata_event_name = self.uploaded_file.metadata.get('event_name', 'Unknown Event')

        # Parse start_date from metadata - support new key and fall back to old key
        metadata_start_date = None
        metadata_start_time = None
        metadata_start_date_str = self.uploaded_file.metadata.get('event_start_date')
        metadata_start_time_str = self.uploaded_file.metadata.get('event_start_time')

        if metadata_start_date_str:
            # New format: separate date and time
            parsed = self.parse_date(metadata_start_date_str)
            if parsed:
                metadata_start_date = parsed.date() if hasattr(parsed, 'date') else parsed
            if metadata_start_time_str:
                time_parsed = self.parse_date(f"2000-01-01 {metadata_start_time_str}")
                if time_parsed:
                    metadata_start_time = time_parsed.time() if hasattr(time_parsed, 'time') else None
        else:
            # Backward compat: old event_date key
            old_event_date_str = self.uploaded_file.metadata.get('event_date')
            if old_event_date_str:
                parsed = self.parse_date(old_event_date_str)
                if parsed:
                    local_dt = timezone.localtime(parsed)
                    metadata_start_date = local_dt.date()
                    metadata_start_time = local_dt.time()

        if not metadata_start_date:
            metadata_start_date = timezone.now().date()

        if not pinned_event:
            # Try to get event info from CSV rows first, fall back to metadata
            event_keys = []
            for row in chunk_data:
                mapped = self.map_columns(row)
                event_name = mapped.get('event_name')
                event_date = self.parse_date(mapped.get('event_date'))
                # If CSV doesn't have event info, use metadata
                if not event_name:
                    event_name = metadata_event_name
                start_date = None
                if event_date:
                    local_dt = timezone.localtime(event_date)
                    start_date = local_dt.date()
                else:
                    start_date = metadata_start_date
                if event_name and start_date:
                    event_keys.append((event_name, start_date))

            # If no event keys found from CSV, use metadata event info
            if not event_keys and metadata_event_name and metadata_start_date:
                event_keys = [(metadata_event_name, metadata_start_date)]


            if event_keys:
                # Get unique event names and dates to query
                unique_names = list(set([key[0] for key in event_keys if key[0]]))
                unique_dates = list(set([key[1] for key in event_keys if key[1]]))

                existing_events_query = Event.objects.filter(
                    name__in=unique_names,
                    start_date__in=unique_dates
                )
                for event in existing_events_query:
                    event_key_tuple = (event.name, event.start_date)
                    existing_events[event_key_tuple] = event
        
        # Check for existing order numbers
        # Note: We need to check all order numbers, not just those in the current chunk,
        # because order numbers from previous files/chunks might conflict
        order_numbers = []
        for row in chunk_data:
            mapped = self.map_columns(row)
            order_num = mapped.get('order_number')
            if not order_num:
                # Generate order number if missing
                order_num = self.generate_order_number(mapped)
            order_numbers.append(order_num)
        
        # Query database for ALL order numbers in this chunk (including generated ones)
        existing_orders = set(
            TicketOrder.objects.filter(order_number__in=order_numbers)
            .values_list('order_number', flat=True)
        )
        
        # If using tiers, sort orders by order_date (earliest first) for tier assignment
        if self.uses_tiers:
            # Sort chunk_data by order_date
            def get_order_date(row):
                mapped = self.map_columns(row)
                order_date = self.parse_date(mapped.get('order_date'))
                return order_date if order_date else timezone.now()
            
            chunk_data = sorted(chunk_data, key=get_order_date)
        
        for row in chunk_data:
            try:
                # Map columns
                mapped_row = self.map_columns(row)
                # Generate order_number if missing
                if not mapped_row.get('order_number'):
                    mapped_row['order_number'] = self.generate_order_number(mapped_row)
                
                # Validate
                is_valid, error_msg = self.validate_ticket_order_data(mapped_row)
                if not is_valid:
                    results['error_count'] += 1
                    results['errors'].append(f"Row validation error: {error_msg}")
                    continue
                
                # Check for duplicate order
                order_number = mapped_row.get('order_number')
                # Double-check database if not in existing_orders (similar to customer issue)
                if order_number in existing_orders:
                    results['skipped_duplicates'] += 1
                    results['skipped_order_numbers'].append(order_number)
                    continue
                else:
                    # Check database directly to catch orders from previous files/chunks
                    if TicketOrder.objects.filter(order_number=order_number).exists():
                        results['skipped_duplicates'] += 1
                        results['skipped_order_numbers'].append(order_number)
                        existing_orders.add(order_number)
                        continue
                
                # Customer: use placeholder for in-person rows, else get or create by email
                if mapped_row.get('processed_in_person') and placeholder_customer is not None:
                    customer = placeholder_customer
                else:
                    if mapped_row.get('processed_in_person') and placeholder_customer is None:
                        results['error_count'] += 1
                        results['errors'].append("Row validation error: In-person row but no organization context.")
                        continue
                    customer_email = mapped_row.get('customer_email', '').lower().strip()
                    customer = existing_customers.get(customer_email)
                    if not customer:
                        # Check database directly before creating (org-scoped)
                        db_filter = Customer.objects.filter(email=customer_email)
                        if org is not None:
                            db_filter = db_filter.filter(organization=org)
                        db_customer = db_filter.first()
                        if db_customer:
                            customer = db_customer
                            existing_customers[customer_email] = customer
                            # Remove from customers_to_create if it was added earlier in this chunk
                            customers_to_create = [c for c in customers_to_create if c.email != customer_email]
                        else:
                            # Check if already in customers_to_create to avoid duplicates within chunk
                            already_in_create = any(c.email == customer_email for c in customers_to_create)
                            if not already_in_create:
                                customer = Customer(
                                    email=customer_email,
                                    name=mapped_row.get('customer_name', ''),
                                    phone=mapped_row.get('customer_phone', ''),
                                    **({'organization': org} if org is not None else {}),
                                )
                                customers_to_create.append(customer)
                                existing_customers[customer_email] = customer
                            else:
                                # Find the customer in customers_to_create
                                customer = next(c for c in customers_to_create if c.email == customer_email)
                    else:
                        # Update customer info if needed
                        if mapped_row.get('customer_name') and customer.name != mapped_row.get('customer_name'):
                            customer.name = mapped_row.get('customer_name')
                            customers_to_update.append(customer)
                        if mapped_row.get('customer_phone') and customer.phone != mapped_row.get('customer_phone'):
                            customer.phone = mapped_row.get('customer_phone')
                            customers_to_update.append(customer)
                
                results['customer_ids'].add(customer.id if customer.id else customer_email)
                
                # Get or create event
                if pinned_event:
                    # Event was specified via event_id metadata — use it directly
                    event = pinned_event
                else:
                    # Use event info from mapped_row, or fall back to metadata/defaults
                    event_name = mapped_row.get('event_name')
                    event_date_raw = self.parse_date(mapped_row.get('event_date'))
                    venue_name = mapped_row.get('venue', '')
                    venue_city = ''

                    # If event fields are missing, try to get from uploaded_file metadata
                    if not event_name:
                        event_name = self.uploaded_file.metadata.get('event_name', 'Unknown Event')

                    # Derive start_date and start_time
                    start_date = None
                    start_time = None
                    if event_date_raw:
                        local_dt = timezone.localtime(event_date_raw)
                        start_date = local_dt.date()
                        start_time = local_dt.time()
                    else:
                        start_date = metadata_start_date
                        start_time = metadata_start_time

                    # Handle venue - try to get from metadata first (new format with venue_id)
                    venue_id = self.uploaded_file.metadata.get('venue_id')
                    if venue_id:
                        try:
                            org = self.uploaded_file.organization
                            venue = Venue.objects.filter(organization=org).get(id=venue_id)
                        except (Venue.DoesNotExist, AttributeError):
                            venue = None
                    else:
                        venue = None

                    # If no venue from metadata, try to parse from CSV or metadata string
                    if not venue:
                        venue_string = venue_name or self.uploaded_file.metadata.get('venue', '')
                        venue_name_meta = self.uploaded_file.metadata.get('venue_name', '')
                        venue_city_meta = self.uploaded_file.metadata.get('venue_city', '')

                        org = getattr(self.uploaded_file, 'organization', None)
                        venue_defaults = lambda n, c: {'name': n, 'city': c}
                        if org:
                            venue_defaults = lambda n, c: {'name': n, 'city': c, 'organization': org}
                        if venue_name_meta and venue_city_meta:
                            # Use metadata venue name and city
                            venue, created = Venue.objects.get_or_create(
                                organization=org,
                                name=venue_name_meta,
                                city=venue_city_meta,
                                defaults=venue_defaults(venue_name_meta, venue_city_meta)
                            ) if org else Venue.objects.get_or_create(
                                name=venue_name_meta,
                                city=venue_city_meta,
                                defaults={'name': venue_name_meta, 'city': venue_city_meta}
                            )
                        elif venue_string:
                            # Try to parse venue string (e.g., "Seattle, WA" or "The Fillmore, San Francisco")
                            if ',' in venue_string:
                                parts = [p.strip() for p in venue_string.rsplit(',', 1)]
                                if len(parts) == 2:
                                    venue_name_parsed = parts[0]
                                    venue_city_parsed = parts[1]
                                else:
                                    venue_name_parsed = venue_string
                                    venue_city_parsed = 'Unknown'
                            else:
                                venue_name_parsed = venue_string
                                venue_city_parsed = 'Unknown'

                            if org:
                                venue, created = Venue.objects.get_or_create(
                                    organization=org,
                                    name=venue_name_parsed,
                                    city=venue_city_parsed,
                                    defaults=venue_defaults(venue_name_parsed, venue_city_parsed)
                                )
                            else:
                                venue, created = Venue.objects.get_or_create(
                                    name=venue_name_parsed,
                                    city=venue_city_parsed,
                                    defaults={'name': venue_name_parsed, 'city': venue_city_parsed}
                                )
                        else:
                            # Default venue
                            if org:
                                venue, created = Venue.objects.get_or_create(
                                    organization=org,
                                    name='Unknown Venue',
                                    city='Unknown',
                                    defaults=venue_defaults('Unknown Venue', 'Unknown')
                                )
                            else:
                                venue, created = Venue.objects.get_or_create(
                                    name='Unknown Venue',
                                    city='Unknown',
                                    defaults={'name': 'Unknown Venue', 'city': 'Unknown'}
                                )

                    event_key = (event_name, start_date)

                    event = existing_events.get(event_key)
                    if not event:
                        event_kwargs = dict(
                            name=event_name,
                            venue=venue,
                            start_date=start_date,
                            start_time=start_time,
                            description=mapped_row.get('event_description', ''),
                        )
                        if org is not None:
                            event_kwargs['organization'] = org
                        event = Event(**event_kwargs)
                        events_to_create.append(event)
                        existing_events[event_key] = event
                
                # Get ticket price
                ticket_type = mapped_row.get('ticket_type', '')
                # Handle multiple ticket types (comma-separated) - use first one
                if ticket_type and ',' in ticket_type:
                    ticket_type = ticket_type.split(',')[0].strip()
                
                quantity = int(mapped_row.get('quantity', 1) or 1)
                
                # Determine price and tier assignment based on mode
                price = None
                assigned_tier = None
                tier_name = None
                
                if self.uses_tiers:
                    # Tier-based pricing mode
                    assigned_tier, tickets_assigned = self._assign_tier_to_ticket(ticket_type, quantity)
                    
                    if assigned_tier is None:
                        # No available tier - reject order
                        results['rejected_orders'].append({
                            'order_number': order_number,
                            'ticket_type': ticket_type,
                            'quantity': quantity,
                            'reason': f"No available tier for ticket type: {ticket_type}"
                        })
                        results['error_count'] += 1
                        continue
                    
                    price = assigned_tier.price
                    tier_name = assigned_tier.name
                else:
                    # Simple pricing mode (current behavior)
                    if mapped_row.get('price'):
                        try:
                            price_value = Decimal(str(mapped_row.get('price')))
                            # If price is from Order Subtotal, divide by quantity to get per-ticket price
                            if quantity > 0:
                                price = price_value / quantity
                            else:
                                price = price_value
                        except (ValueError, TypeError, ZeroDivisionError):
                            pass
                    
                    if price is None and manual_prices:
                        price = manual_prices.get(ticket_type)
                    
                    # If still no price, try to calculate from total_amount
                    if price is None:
                        total_amount = mapped_row.get('total_amount')
                        if total_amount:
                            try:
                                total = Decimal(str(total_amount))
                                if quantity > 0:
                                    price = total / quantity
                            except (ValueError, TypeError, ZeroDivisionError):
                                pass
                    
                    # If price is still None, default to 0 for free tickets
                    if price is None:
                        price = Decimal('0.00')
                
                # Calculate total amount
                total_amount = None
                if mapped_row.get('total_amount'):
                    try:
                        total_amount = Decimal(str(mapped_row.get('total_amount')))
                    except (ValueError, TypeError):
                        pass
                
                if total_amount is None:
                    # Calculate from ticket price × quantity
                    total_amount = price * quantity
                
                # Create ticket order
                order_date = self.parse_date(mapped_row.get('order_date'))
                ticket_order = TicketOrder(
                    customer=customer,
                    event=event,
                    uploaded_file=self.uploaded_file,
                    order_number=order_number,
                    order_date=order_date,
                    total_amount=total_amount
                )
                ticket_orders_to_create.append(ticket_order)
                
                # Create tickets
                for _ in range(quantity):
                    ticket = Ticket(
                        ticket_order=ticket_order,
                        ticket_type=ticket_type,
                        price=price,
                        tier=assigned_tier,
                        tier_name=tier_name
                    )
                    tickets_to_create.append(ticket)
                
                existing_orders.add(order_number)
                results['success_count'] += 1
                
            except Exception as e:
                logger.error(f"Error processing row: {str(e)}", exc_info=True)
                results['error_count'] += 1
                results['errors'].append(f"Row processing error: {str(e)}")
        
        # Bulk create/update
        if customers_to_create:
            # Final check: remove any customers that exist in database (org-scoped)
            final_emails = [c.email for c in customers_to_create]
            existing_in_db_qs = Customer.objects.filter(email__in=final_emails)
            if org is not None:
                existing_in_db_qs = existing_in_db_qs.filter(organization=org)
            existing_in_db = {c.email: c for c in existing_in_db_qs}
            if existing_in_db:
                customers_to_create = [c for c in customers_to_create if c.email not in existing_in_db]
            if customers_to_create:
                Customer.objects.bulk_create(customers_to_create)
            # Refetch created customers to get their IDs
            created_emails = [c.email for c in customers_to_create]
            created_qs = Customer.objects.filter(email__in=created_emails)
            if org is not None:
                created_qs = created_qs.filter(organization=org)
            created_customers = {c.email: c for c in created_qs}
            # Update existing_customers dict with newly created customers
            for email, customer in created_customers.items():
                existing_customers[email] = customer
                # Update customer_ids with actual IDs
                if email in results['customer_ids']:
                    results['customer_ids'].remove(email)
                    results['customer_ids'].add(customer.id)
        
        if customers_to_update:
            Customer.objects.bulk_update(customers_to_update, ['name', 'phone'])
        if events_to_create:
            Event.objects.bulk_create(events_to_create, ignore_conflicts=True)
            # Refetch created events to get their IDs
            created_event_keys = [(e.name, e.start_date) for e in events_to_create]
            created_events = {}
            event_filter = Event.objects.filter(
                name__in=[key[0] for key in created_event_keys if key[0]]
            )
            if org is not None:
                event_filter = event_filter.filter(organization=org)
            for event in event_filter:
                event_key_tuple = (event.name, event.start_date)
                created_events[event_key_tuple] = event
            # Update existing_events dict
            for key, event in created_events.items():
                existing_events[key] = event
        
        if ticket_orders_to_create:
            # Final safety check: remove any orders that exist in database
            final_order_numbers = [o.order_number for o in ticket_orders_to_create]
            existing_orders_in_db = set(
                TicketOrder.objects.filter(order_number__in=final_order_numbers)
                .values_list('order_number', flat=True)
            )
            if existing_orders_in_db:
                ticket_orders_to_create = [o for o in ticket_orders_to_create if o.order_number not in existing_orders_in_db]
        if ticket_orders_to_create:
            TicketOrder.objects.bulk_create(ticket_orders_to_create)
        if tickets_to_create:
            Ticket.objects.bulk_create(tickets_to_create)
        
        return results
    
    def _update_customer_ltv(self, customer_ids: set):
        """Update customer lifetime value for processed customers."""
        for customer_id in customer_ids:
            try:
                if isinstance(customer_id, str):
                    # Email lookup
                    customer = Customer.objects.filter(email=customer_id).first()
                else:
                    customer = Customer.objects.filter(id=customer_id).first()
                
                if customer:
                    customer.update_lifetime_value()
            except Exception as e:
                logger.error(f"Error updating LTV for customer {customer_id}: {str(e)}")
