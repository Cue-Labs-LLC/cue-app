import json
from django import forms
from django.contrib.auth.forms import AuthenticationForm
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Row, Column, Submit, Field
from .models import CSVFormat, Venue


class LoginForm(AuthenticationForm):
    """Custom login form with Bootstrap styling."""
    
    username = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Username',
            'autofocus': True
        })
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Password'
        })
    )
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field('username'),
            Field('password'),
            Submit('submit', 'Login', css_class='btn btn-primary w-100')
        )


class PrettyJSONField(forms.JSONField):
    """Custom JSONField that formats JSON with indentation for display."""
    def prepare_value(self, value):
        """Format the value as pretty JSON when preparing for widget display."""
        if value is None:
            return ''
        # If it's already a dict/list, format it with indentation
        if isinstance(value, (dict, list)):
            return json.dumps(value, indent=2)
        # If it's a string, try to parse and reformat it
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return ''
            try:
                # Try to parse as JSON
                parsed = json.loads(value)
                return json.dumps(parsed, indent=2)
            except (json.JSONDecodeError, TypeError, ValueError):
                # If parsing fails, return as-is (might be user input in progress)
                return value
        return str(value)


class JSONTextarea(forms.Textarea):
    """Custom textarea widget that formats JSON properly."""
    def format_value(self, value):
        """Format the value as pretty JSON."""
        if value is None:
            return ''
        # If it's already a dict, format it
        if isinstance(value, dict):
            return json.dumps(value, indent=2)
        # If it's a string, try to parse and reformat it
        if isinstance(value, str):
            # Remove any leading/trailing whitespace
            value = value.strip()
            if not value:
                return ''
            try:
                # Try to parse as JSON
                parsed = json.loads(value)
                return json.dumps(parsed, indent=2)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                # If parsing fails, return as-is (might be user input in progress)
                return value
        return str(value)


class CSVUploadForm(forms.Form):
    """Form for uploading CSV files with metadata."""
    csv_file = forms.FileField(
        label="CSV File",
        help_text="Upload a CSV file (max 10MB)",
        widget=forms.FileInput(attrs={'accept': '.csv', 'class': 'form-control'})
    )
    csv_format = forms.ModelChoiceField(
        queryset=CSVFormat.objects.all(),
        label="CSV Format",
        help_text="Select the format configuration for this CSV file",
        empty_label="Select a format...",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    event_name = forms.CharField(
        required=True,
        max_length=200,
        label="Event Name",
        help_text="Enter the name of the event",
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Familiar Faces'})
    )
    event_date = forms.DateTimeField(
        required=True,
        label="Event Date",
        help_text="Date and time of the event",
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'})
    )
    venue = forms.ModelChoiceField(
        queryset=Venue.objects.all().order_by('name', 'city'),
        required=True,
        label="Venue",
        help_text="Select an existing venue",
        widget=forms.Select(attrs={
            'class': 'form-select',
            'data-placeholder': 'Select a venue...'
        })
    )
    notes = forms.CharField(
        required=False,
        label="Notes",
        widget=forms.Textarea(attrs={'rows': 2, 'class': 'form-control'})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field('csv_file'),
            Field('csv_format'),
            Row(
                Column('event_name', css_class='form-group col-md-12 mb-0'),
            ),
            Row(
                Column('event_date', css_class='form-group col-md-6 mb-0'),
                Column('venue', css_class='form-group col-md-6 mb-0'),
            ),
            Field('notes'),
            Submit('submit', 'Upload CSV', css_class='btn btn-primary')
        )

        # Auto-select default format if available
        default_format = CSVFormat.objects.filter(is_default=True).first()
        if default_format:
            self.fields['csv_format'].initial = default_format

    def clean(self):
        """Validate form data."""
        cleaned_data = super().clean()
        venue = cleaned_data.get('venue')
        
        # Venue must be selected
        if not venue:
            raise forms.ValidationError("Please select a venue.")
        
        return cleaned_data

    def clean_csv_file(self):
        """Validate CSV file."""
        file = self.cleaned_data.get('csv_file')
        if file:
            # Check file size (10MB)
            if file.size > 10 * 1024 * 1024:
                raise forms.ValidationError("File size exceeds 10MB limit.")
            # Check file extension
            if not file.name.lower().endswith('.csv'):
                raise forms.ValidationError("File must be a CSV file.")
        return file


class TicketPriceEntryForm(forms.Form):
    """Dynamic form for manually entering ticket prices or tiers."""
    
    def __init__(self, ticket_types, uses_tiers=False, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.uses_tiers = uses_tiers
        self.ticket_types = ticket_types
        self.helper = FormHelper()
        self.helper.layout = Layout()
        
        if uses_tiers:
            # Tier-based entry mode
            # Fields are created dynamically by JavaScript
            # Add all tier fields from POST data as optional fields so form validation passes
            data = kwargs.get('data') or args[0] if args else {}
            if data:
                for key in data.keys():
                    if key.startswith('tier_') and key not in self.fields and not key.startswith('tier_count_'):
                        # Determine field type based on suffix
                        if key.endswith('_name'):
                            self.fields[key] = forms.CharField(required=False)
                        elif key.endswith('_price'):
                            self.fields[key] = forms.DecimalField(required=False, max_digits=10, decimal_places=2, min_value=0)
                        elif key.endswith('_allotment'):
                            self.fields[key] = forms.IntegerField(required=False, min_value=1)
                        elif key.endswith('_order'):
                            self.fields[key] = forms.IntegerField(required=False, min_value=1)
            
            layout_fields = []
            layout_fields.append(Submit('submit', 'Process with Tiers', css_class='btn btn-primary'))
            self.helper.layout = Layout(*layout_fields)
        else:
            # Simple price entry mode (current behavior)
            layout_fields = []
            for ticket_type in ticket_types:
                field_name = f"price_{ticket_type.replace(' ', '_').replace('-', '_')}"
                self.fields[field_name] = forms.DecimalField(
                    label=ticket_type,
                    max_digits=10,
                    decimal_places=2,
                    min_value=0,
                    widget=forms.NumberInput(attrs={
                        'class': 'form-control',
                        'step': '0.01',
                        'placeholder': '0.00'
                    })
                )
                layout_fields.append(Field(field_name))
            
            layout_fields.append(Submit('submit', 'Process with Prices', css_class='btn btn-primary'))
            self.helper.layout = Layout(*layout_fields)

    def get_prices_dict(self):
        """Extract prices as a dictionary keyed by ticket type (simple mode)."""
        if self.uses_tiers:
            return {}  # Not used in tier mode
        
        prices = {}
        for field_name, value in self.cleaned_data.items():
            if field_name.startswith('price_'):
                # Convert field name back to ticket type
                ticket_type = field_name.replace('price_', '').replace('_', ' ')
                prices[ticket_type] = value
        return prices
    
    def get_tier_definitions_dict(self):
        """Extract tier definitions as nested dictionary (tier mode)."""
        if not self.uses_tiers:
            return {}  # Not used in simple mode
        
        tier_definitions = {}
        
        # Parse tier data from form submission
        # Format: tier_{ticket_type}_{index}_{field} = value
        # e.g., tier_General_Admission_0_name = "Early Bird"
        #       tier_General_Admission_0_price = 25.00
        #       tier_General_Admission_0_allotment = 100
        #       tier_General_Admission_0_order = 1
        
        for field_name, value in self.cleaned_data.items():
            if field_name.startswith('tier_') and not field_name.startswith('tier_count_'):
                # Parse: tier_{ticket_type}_{index}_{field}
                parts = field_name.replace('tier_', '').split('_')
                if len(parts) >= 3:
                    # Reconstruct ticket type (may contain underscores)
                    # Find the last two parts (index and field)
                    index = parts[-2]
                    field = parts[-1]
                    ticket_type_parts = parts[:-2]
                    ticket_type = ' '.join(ticket_type_parts).replace('_', ' ')
                    
                    if ticket_type not in tier_definitions:
                        tier_definitions[ticket_type] = []
                    
                    # Ensure we have enough entries
                    idx = int(index)
                    while len(tier_definitions[ticket_type]) <= idx:
                        tier_definitions[ticket_type].append({
                            'name': '',
                            'price': None,
                            'allotment': None,
                            'order': None
                        })
                    
                    # Set the field value
                    if field == 'name':
                        tier_definitions[ticket_type][idx]['name'] = value
                    elif field == 'price':
                        tier_definitions[ticket_type][idx]['price'] = value
                    elif field == 'allotment':
                        tier_definitions[ticket_type][idx]['allotment'] = value
                    elif field == 'order':
                        tier_definitions[ticket_type][idx]['order'] = value
        
        # Clean up and validate tier definitions
        cleaned_definitions = {}
        for ticket_type, tiers in tier_definitions.items():
            # Filter out incomplete tiers and sort by order
            valid_tiers = [
                tier for tier in tiers
                if tier['name'] and tier['price'] is not None and 
                   tier['allotment'] is not None and tier['order'] is not None
            ]
            if valid_tiers:
                # Sort by order
                valid_tiers.sort(key=lambda x: int(x['order']))
                cleaned_definitions[ticket_type] = valid_tiers
        
        return cleaned_definitions


class CSVFormatForm(forms.ModelForm):
    """Form for creating/editing CSV format configurations."""
    
    # Override column_mapping field to use PrettyJSONField
    column_mapping = PrettyJSONField(
        widget=JSONTextarea(attrs={
            'rows': 15,
            'class': 'form-control font-monospace',
            'placeholder': '{\n  "order_number": ["order_id", "order_number"],\n  "customer_email": ["email", "customer_email"],\n  ...\n}'
        })
    )
    
    class Meta:
        model = CSVFormat
        fields = ['name', 'description', 'is_default', 'requires_manual_pricing', 'uses_tiers', 'column_mapping']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
            'is_default': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'requires_manual_pricing': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'uses_tiers': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Ensure column_mapping is formatted as pretty JSON
        # The widget will handle formatting, but we also set initial value here
        if self.instance and self.instance.pk:
            if hasattr(self.instance, 'column_mapping') and self.instance.column_mapping:
                if isinstance(self.instance.column_mapping, dict):
                    self.initial['column_mapping'] = json.dumps(self.instance.column_mapping, indent=2)
                elif isinstance(self.instance.column_mapping, str):
                    # Try to parse and reformat if it's a string
                    try:
                        parsed = json.loads(self.instance.column_mapping)
                        self.initial['column_mapping'] = json.dumps(parsed, indent=2)
                    except (json.JSONDecodeError, TypeError):
                        pass
        
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field('name'),
            Field('description'),
            Row(
                Column(Field('is_default', css_class='form-check-input'), css_class='form-group col-md-4 mb-0'),
                Column(Field('requires_manual_pricing', css_class='form-check-input'), css_class='form-group col-md-4 mb-0'),
                Column(Field('uses_tiers', css_class='form-check-input'), css_class='form-group col-md-4 mb-0'),
            ),
            Field('column_mapping'),
            Submit('submit', 'Save Format', css_class='btn btn-primary')
        )
        
        # Add help text for uses_tiers
        self.fields['uses_tiers'].help_text = "Enable tier-based pricing with allotments. Only available when manual pricing is required."

    def clean_column_mapping(self):
        """Validate column mapping JSON."""
        import json
        column_mapping = self.cleaned_data.get('column_mapping')
        if isinstance(column_mapping, str):
            try:
                column_mapping = json.loads(column_mapping)
            except json.JSONDecodeError:
                raise forms.ValidationError("Invalid JSON format for column mapping.")
        
        # Validate required fields (event fields can be in metadata)
        required_fields = [
            'order_number', 'order_date', 'customer_email', 'customer_name',
            'ticket_type'
        ]
        
        missing_fields = [field for field in required_fields if field not in column_mapping]
        if missing_fields:
            raise forms.ValidationError(
                f"Missing required field mappings: {', '.join(missing_fields)}"
            )
        
        # Warn if event fields are missing (they can be provided via metadata)
        optional_event_fields = ['event_name', 'event_date', 'venue']
        missing_event_fields = [field for field in optional_event_fields if field not in column_mapping or not column_mapping.get(field)]
        if missing_event_fields:
            # This is a warning, not an error - event info can come from metadata
            pass
        
        # If requires_manual_pricing is False, check for price/total_amount mappings
        if not self.cleaned_data.get('requires_manual_pricing', False):
            if 'price' not in column_mapping and 'total_amount' not in column_mapping:
                raise forms.ValidationError(
                    "Format must include 'price' or 'total_amount' mapping if manual pricing is not required."
                )
        
        # Validate uses_tiers can only be True if requires_manual_pricing is True
        uses_tiers = self.cleaned_data.get('uses_tiers', False)
        requires_manual_pricing = self.cleaned_data.get('requires_manual_pricing', False)
        if uses_tiers and not requires_manual_pricing:
            raise forms.ValidationError(
                "Tier-based pricing can only be enabled when manual pricing is required."
            )
        
        return column_mapping
    
    def clean_uses_tiers(self):
        """Ensure uses_tiers is False if requires_manual_pricing is False."""
        uses_tiers = self.cleaned_data.get('uses_tiers', False)
        requires_manual_pricing = self.cleaned_data.get('requires_manual_pricing', False)
        
        if uses_tiers and not requires_manual_pricing:
            return False  # Force to False if manual pricing is not required
        
        return uses_tiers


class VenueForm(forms.ModelForm):
    """Form for creating/editing venues."""
    
    class Meta:
        model = Venue
        fields = ['name', 'city']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., The Fillmore'}),
            'city': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., San Francisco'}),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field('name'),
            Field('city'),
            Submit('submit', 'Create Venue', css_class='btn btn-primary')
        )
