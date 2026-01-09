import csv
import json
import logging
from decimal import Decimal
from datetime import datetime
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

# #region agent log
DEBUG_LOG_PATH = r'c:\Users\Owen1\dev\github.com\personal\enhanced-ltv-updater\.cursor\debug.log'
# #endregion


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
                parsed = timezone.make_aware(parsed, timezone.utc)
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
    
    def validate_ticket_order_data(self, row: Dict) -> Tuple[bool, Optional[str]]:
        """Validate individual ticket order row."""
        # Core required fields (order_number can be auto-generated, event fields can come from metadata)
        required_fields = ['order_date', 'customer_email', 
                          'customer_name', 'ticket_type']
        
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
            'rejected_orders': []
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
        results = {
            'success_count': 0,
            'error_count': 0,
            'skipped_duplicates': 0,
            'errors': [],
            'skipped_order_numbers': [],
            'customer_ids': set(),
            'rejected_orders': []
        }
        
        customers_to_create = []
        customers_to_update = []
        events_to_create = []
        events_to_update = []
        ticket_orders_to_create = []
        tickets_to_create = []
        
        # Track existing customers and events for bulk operations
        # #region agent log
        chunk_emails = [row.get('customer_email', '').lower().strip() for row in chunk_data if row.get('customer_email')]
        with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "services.py:332", "message": "Querying existing customers for chunk", "data": {"chunk_emails_count": len(chunk_emails), "chunk_emails_sample": chunk_emails[:5], "uploaded_file_id": str(self.uploaded_file.id)}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
        # #endregion
        existing_customers = {
            c.email: c for c in Customer.objects.filter(
                email__in=chunk_emails
            )
        }
        # #region agent log
        with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "services.py:337", "message": "Found existing customers", "data": {"found_count": len(existing_customers), "found_emails": list(existing_customers.keys())[:5]}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
        # #endregion
        
        existing_events = {}
        # Pre-fetch existing events for this chunk
        # Get event info from metadata (used when CSV doesn't have event columns)
        metadata_event_name = self.uploaded_file.metadata.get('event_name', 'Unknown Event')
        metadata_event_date_str = self.uploaded_file.metadata.get('event_date')
        metadata_event_date = None
        if metadata_event_date_str:
            metadata_event_date = self.parse_date(metadata_event_date_str)
        else:
            metadata_event_date = timezone.now()
        
        # Try to get event info from CSV rows first, fall back to metadata
        event_keys = []
        for row in chunk_data:
            mapped = self.map_columns(row)
            event_name = mapped.get('event_name')
            event_date = self.parse_date(mapped.get('event_date'))
            # If CSV doesn't have event info, use metadata
            if not event_name:
                event_name = metadata_event_name
            if not event_date:
                event_date = metadata_event_date
            if event_name and event_date:
                event_keys.append((event_name, event_date))
        
        # If no event keys found from CSV, use metadata event info
        if not event_keys and metadata_event_name and metadata_event_date:
            event_keys = [(metadata_event_name, metadata_event_date)]
        
        
        if event_keys:
            # Get unique event names and dates to query
            unique_names = list(set([key[0] for key in event_keys if key[0]]))
            unique_dates = list(set([key[1] for key in event_keys if key[1]]))
            
            existing_events_query = Event.objects.filter(
                name__in=unique_names,
                event_date__in=unique_dates
            )
            for event in existing_events_query:
                event_key_tuple = (event.name, event.event_date)
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
        # #region agent log
        with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "B", "location": "services.py:400", "message": "Checked existing order numbers", "data": {"chunk_order_count": len(order_numbers), "existing_count": len(existing_orders), "existing_sample": list(existing_orders)[:5]}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
        # #endregion
        
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
                        # #region agent log
                        with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
                            f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "B", "location": "services.py:432", "message": "Order number found in database but not in existing_orders", "data": {"order_number": order_number}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
                        # #endregion
                        results['skipped_duplicates'] += 1
                        results['skipped_order_numbers'].append(order_number)
                        existing_orders.add(order_number)
                        continue
                
                # Get or create customer
                customer_email = mapped_row.get('customer_email', '').lower().strip()
                # #region agent log
                with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
                    f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "services.py:421", "message": "Processing customer email", "data": {"customer_email": customer_email, "in_existing_customers": customer_email in existing_customers, "order_number": mapped_row.get('order_number')}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
                # #endregion
                customer = existing_customers.get(customer_email)
                if not customer:
                    # #region agent log
                    with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
                        f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "services.py:424", "message": "Customer not found, checking database", "data": {"customer_email": customer_email}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
                    # #endregion
                    # Check database directly before creating (HYPOTHESIS A: existing_customers only queries current chunk)
                    db_customer = Customer.objects.filter(email=customer_email).first()
                    if db_customer:
                        # #region agent log
                        with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
                            f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "services.py:430", "message": "Customer found in database but not in existing_customers", "data": {"customer_email": customer_email, "customer_id": str(db_customer.id)}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
                        # #endregion
                        customer = db_customer
                        existing_customers[customer_email] = customer
                        # Remove from customers_to_create if it was added earlier in this chunk
                        before_count = len(customers_to_create)
                        customers_to_create = [c for c in customers_to_create if c.email != customer_email]
                        after_count = len(customers_to_create)
                        # #region agent log
                        if before_count != after_count:
                            with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
                                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "services.py:455", "message": "Removed customer from customers_to_create", "data": {"customer_email": customer_email, "before_count": before_count, "after_count": after_count}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
                        # #endregion
                    else:
                        # Check if already in customers_to_create to avoid duplicates within chunk
                        already_in_create = any(c.email == customer_email for c in customers_to_create)
                        # #region agent log
                        with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
                            f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "services.py:435", "message": "Customer not in database, adding to create list", "data": {"customer_email": customer_email, "already_in_create_list": already_in_create}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
                        # #endregion
                        if not already_in_create:
                            customer = Customer(
                                email=customer_email,
                                name=mapped_row.get('customer_name', ''),
                                phone=mapped_row.get('customer_phone', '')
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
                # Use event info from mapped_row, or fall back to metadata/defaults
                event_name = mapped_row.get('event_name')
                event_date = self.parse_date(mapped_row.get('event_date'))
                venue_name = mapped_row.get('venue', '')
                venue_city = ''
                
                # If event fields are missing, try to get from uploaded_file metadata
                if not event_name:
                    event_name = self.uploaded_file.metadata.get('event_name', 'Unknown Event')
                if not event_date:
                    event_date_str = self.uploaded_file.metadata.get('event_date')
                    if event_date_str:
                        event_date = self.parse_date(event_date_str)
                    else:
                        # Use a default date if not provided
                        event_date = timezone.now()
                
                # Handle venue - try to get from metadata first (new format with venue_id)
                venue_id = self.uploaded_file.metadata.get('venue_id')
                if venue_id:
                    try:
                        venue = Venue.objects.get(id=venue_id)
                    except Venue.DoesNotExist:
                        venue = None
                else:
                    venue = None
                
                # If no venue from metadata, try to parse from CSV or metadata string
                if not venue:
                    venue_string = venue_name or self.uploaded_file.metadata.get('venue', '')
                    venue_name_meta = self.uploaded_file.metadata.get('venue_name', '')
                    venue_city_meta = self.uploaded_file.metadata.get('venue_city', '')
                    
                    if venue_name_meta and venue_city_meta:
                        # Use metadata venue name and city
                        venue, created = Venue.objects.get_or_create(
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
                        
                        venue, created = Venue.objects.get_or_create(
                            name=venue_name_parsed,
                            city=venue_city_parsed,
                            defaults={'name': venue_name_parsed, 'city': venue_city_parsed}
                        )
                    else:
                        # Default venue
                        venue, created = Venue.objects.get_or_create(
                            name='Unknown Venue',
                            city='Unknown',
                            defaults={'name': 'Unknown Venue', 'city': 'Unknown'}
                        )
                
                event_key = (event_name, event_date)
                
                event = existing_events.get(event_key)
                if not event:
                    event = Event(
                        name=event_name,
                        venue=venue,
                        event_date=event_date,
                        description=mapped_row.get('event_description', '')
                    )
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
            # Final check: remove any customers that exist in database
            final_emails = [c.email for c in customers_to_create]
            existing_in_db = {c.email: c for c in Customer.objects.filter(email__in=final_emails)}
            if existing_in_db:
                # #region agent log
                with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
                    f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "services.py:636", "message": "Found existing customers in database before bulk_create", "data": {"existing_emails": list(existing_in_db.keys()), "customers_to_create_count": len(customers_to_create)}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
                # #endregion
                customers_to_create = [c for c in customers_to_create if c.email not in existing_in_db]
            # #region agent log
            with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "services.py:642", "message": "About to bulk_create customers", "data": {"count": len(customers_to_create), "emails": [c.email for c in customers_to_create][:10]}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
            # #endregion
            if customers_to_create:
                try:
                    Customer.objects.bulk_create(customers_to_create)
                except Exception as e:
                    # #region agent log
                    with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
                        f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "services.py:695", "message": "bulk_create failed", "data": {"error": str(e), "error_type": type(e).__name__, "emails": [c.email for c in customers_to_create][:10]}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
                    # #endregion
                    raise
            # Refetch created customers to get their IDs
            created_emails = [c.email for c in customers_to_create]
            created_customers = {
                c.email: c for c in Customer.objects.filter(email__in=created_emails)
            }
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
            created_event_keys = [(e.name, e.event_date) for e in events_to_create]
            created_events = {}
            for event in Event.objects.filter(
                name__in=[key[0] for key in created_event_keys if key[0]]
            ):
                event_key_tuple = (event.name, event.event_date)
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
                # #region agent log
                with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
                    f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "B", "location": "services.py:760", "message": "Found existing orders in database before bulk_create", "data": {"existing_order_numbers": list(existing_orders_in_db), "ticket_orders_to_create_count": len(ticket_orders_to_create)}, "timestamp": int(timezone.now().timestamp() * 1000)}) + "\n")
                # #endregion
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
