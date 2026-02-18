import json
from django import forms
from django.forms import modelformset_factory
from django.contrib.auth.forms import AuthenticationForm
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Row, Column, Submit, Field
from .models import Organization, CSVFormat, Venue, Event, EventTalent, EventExpense, CustomField, IncomeSource, EventIncome


class OrganizationForm(forms.ModelForm):
    """Form for creating a new organization."""

    class Meta:
        model = Organization
        fields = ['name', 'slug']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Acme Events'}),
            'slug': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., acme-events'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            Field('name'),
            Field('slug'),
            Submit('submit', 'Create Organization', css_class='btn btn-primary'),
        )


class MemberInviteForm(forms.Form):
    """Form to invite a member to the organization by email."""

    email = forms.EmailField(
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'Email address',
        })
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            Field('email'),
            Submit('submit', 'Invite member', css_class='btn btn-primary'),
        )


class LoginForm(AuthenticationForm):
    """Custom login form with Bootstrap styling (email-only login)."""

    username = forms.EmailField(
        label='Email',
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'Email',
            'autofocus': True,
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


class SignUpForm(forms.Form):
    """Sign-up form collecting email, name, and password."""

    email = forms.EmailField(
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'Email address',
            'autofocus': True,
        })
    )
    first_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'First name',
        })
    )
    last_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Last name',
        })
    )
    password1 = forms.CharField(
        label='Password',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Password',
        })
    )
    password2 = forms.CharField(
        label='Confirm password',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Confirm password',
        })
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field('email'),
            Row(
                Column('first_name', css_class='form-group col-md-6 mb-0'),
                Column('last_name', css_class='form-group col-md-6 mb-0'),
            ),
            Field('password1'),
            Field('password2'),
            Submit('submit', 'Sign Up', css_class='btn btn-primary w-100'),
        )

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get('password1')
        p2 = cleaned_data.get('password2')
        if p1 and p2 and p1 != p2:
            self.add_error('password2', 'Passwords do not match.')
        return cleaned_data

    def clean_password1(self):
        from django.contrib.auth.password_validation import validate_password
        password = self.cleaned_data.get('password1')
        if password:
            validate_password(password)
        return password


class OTPVerificationForm(forms.Form):
    """Form for entering a 6-digit OTP code."""

    otp_code = forms.CharField(
        label='Verification code',
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg text-center',
            'placeholder': '000000',
            'inputmode': 'numeric',
            'pattern': '[0-9]{6}',
            'autocomplete': 'one-time-code',
            'autofocus': True,
        })
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field('otp_code'),
            Submit('submit', 'Verify', css_class='btn btn-primary w-100'),
        )

    def clean_otp_code(self):
        code = self.cleaned_data.get('otp_code', '').strip()
        if not code.isdigit():
            raise forms.ValidationError('Code must be 6 digits.')
        return code


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
    event_start_date = forms.DateField(
        required=True,
        label="Event Start Date",
        help_text="Date of the event",
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'})
    )
    event_start_time = forms.TimeField(
        required=False,
        label="Event Start Time",
        help_text="Time of the event (optional)",
        widget=forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'})
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
        organization = kwargs.pop('organization', None)
        super().__init__(*args, **kwargs)
        if organization is not None:
            self.fields['csv_format'].queryset = CSVFormat.objects.filter(
                organization=organization
            ).order_by('-is_default', 'name')
            self.fields['venue'].queryset = Venue.objects.filter(
                organization=organization
            ).order_by('name', 'city')
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field('csv_file'),
            Field('csv_format'),
            Row(
                Column('event_name', css_class='form-group col-md-12 mb-0'),
            ),
            Row(
                Column('event_start_date', css_class='form-group col-md-4 mb-0'),
                Column('event_start_time', css_class='form-group col-md-4 mb-0'),
                Column('venue', css_class='form-group col-md-4 mb-0'),
            ),
            Field('notes'),
            Submit('submit', 'Upload CSV', css_class='btn btn-primary')
        )

        # Auto-select default format if available (org-scoped)
        if organization is not None:
            default_format = CSVFormat.objects.filter(
                organization=organization, is_default=True
            ).first()
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


class EventCSVUploadForm(forms.Form):
    """Simplified CSV upload form for uploading directly from an event detail page."""
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
    notes = forms.CharField(
        required=False,
        label="Notes",
        widget=forms.Textarea(attrs={'rows': 2, 'class': 'form-control'})
    )

    def __init__(self, *args, **kwargs):
        organization = kwargs.pop('organization', None)
        super().__init__(*args, **kwargs)
        if organization is not None:
            self.fields['csv_format'].queryset = CSVFormat.objects.filter(
                organization=organization
            ).order_by('-is_default', 'name')
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field('csv_file'),
            Field('csv_format'),
            Field('notes'),
            Submit('submit', 'Upload CSV', css_class='btn btn-primary')
        )

        if organization is not None:
            default_format = CSVFormat.objects.filter(
                organization=organization, is_default=True
            ).first()
            if default_format:
                self.fields['csv_format'].initial = default_format

    def clean_csv_file(self):
        file = self.cleaned_data.get('csv_file')
        if file:
            if file.size > 10 * 1024 * 1024:
                raise forms.ValidationError("File size exceeds 10MB limit.")
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
        # Optional: document in-person support for column_mapping
        self.fields['column_mapping'].help_text = (
            "Required keys: order_number, order_date, customer_email, customer_name, ticket_type. "
            "Optional: add \"processed_in_person\": [\"Was Processed In Person\"] for exports that include in-person sales; "
            "rows with that column set to true can omit email/name and will be attributed to \"In-Person Sales\"."
        )

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
        fields = [
            'name', 'city', 'street_address', 'state', 'postal_code', 'country',
            'capacity',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., The Fillmore'}),
            'city': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., San Francisco'}),
            'street_address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., 123 Main St', 'autocomplete': 'off'}),
            'state': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., CA'}),
            'postal_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., 94102'}),
            'country': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., USA'}),
            'capacity': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'e.g., 500', 'min': '1'}),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['capacity'].required = False
        submit_label = 'Update Venue' if (self.instance and self.instance.pk) else 'Create Venue'
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field('name'),
            Field('city'),
            Field('street_address'),
            Field('state'),
            Field('postal_code'),
            Field('country'),
            Field('capacity'),
            Submit('submit', submit_label, css_class='btn btn-primary')
        )


class EventExpenseForm(forms.ModelForm):
    """Form for creating/editing event expenses."""

    class Meta:
        model = EventExpense
        fields = ['category', 'description', 'amount', 'expense_date', 'notes']
        widgets = {
            'category': forms.Select(attrs={'class': 'form-select'}),
            'description': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., DJ fee for headliner'}),
            'amount': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'placeholder': '0.00'}),
            'expense_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'notes': forms.Textarea(attrs={'rows': 2, 'class': 'form-control', 'placeholder': 'Optional notes'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['notes'].required = False
        self.fields['expense_date'].required = False
        submit_label = 'Update Expense' if (self.instance and self.instance.pk) else 'Add Expense'
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Row(
                Column('category', css_class='form-group col-md-4 mb-0'),
                Column('amount', css_class='form-group col-md-4 mb-0'),
                Column('expense_date', css_class='form-group col-md-4 mb-0'),
            ),
            Field('description'),
            Field('notes'),
            Submit('submit', submit_label, css_class='btn btn-primary'),
        )


class IncomeSourceForm(forms.ModelForm):
    """Form for creating/editing organization-level income source types."""

    class Meta:
        model = IncomeSource
        fields = ['name', 'order']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Bar Splits'}),
            'order': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        submit_label = 'Update Income Source' if (self.instance and self.instance.pk) else 'Add Income Source'
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Field('name'),
            Field('order'),
            Submit('submit', submit_label, css_class='btn btn-primary'),
        )


class EventIncomeForm(forms.ModelForm):
    """Form for adding/editing additional income on an event."""

    class Meta:
        model = EventIncome
        fields = ['income_source', 'amount', 'income_date', 'notes']
        widgets = {
            'income_source': forms.Select(attrs={'class': 'form-select'}),
            'amount': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'placeholder': '0.00'}),
            'income_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'notes': forms.Textarea(attrs={'rows': 2, 'class': 'form-control', 'placeholder': 'Optional notes'}),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['notes'].required = False
        self.fields['income_date'].required = False
        if organization is not None:
            self.fields['income_source'].queryset = IncomeSource.objects.filter(
                organization=organization
            ).order_by('order', 'name')
        submit_label = 'Update Income' if (self.instance and self.instance.pk) else 'Add Income'
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column('income_source', css_class='form-group col-md-6 mb-0'),
                Column('amount', css_class='form-group col-md-3 mb-0'),
                Column('income_date', css_class='form-group col-md-3 mb-0'),
            ),
            Field('notes'),
            Submit('submit', submit_label, css_class='btn btn-primary'),
        )


class EventTalentForm(forms.ModelForm):
    """Single talent row for formset; name optional so extra rows can be left blank."""
    class Meta:
        model = EventTalent
        fields = ('name', 'order')
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., DJ Shadow'}),
            'order': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].required = False


EventTalentFormSet = modelformset_factory(
    EventTalent,
    form=EventTalentForm,
    extra=0,
    can_delete=True,
)


class EventForm(forms.ModelForm):
    """Form for creating events."""

    class Meta:
        model = Event
        fields = [
            'name', 'venue', 'start_date', 'start_time', 'end_date', 'end_time',
            'description', 'capacity', 'timezone', 'ticket_link',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Familiar Faces'}),
            'venue': forms.Select(attrs={'class': 'form-select'}),
            'start_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'start_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'}),
            'end_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'end_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'}),
            'description': forms.Textarea(attrs={'rows': 3, 'class': 'form-control', 'placeholder': 'Optional event description'}),
            'capacity': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'e.g., 500', 'min': '1'}),
            'timezone': forms.Select(attrs={'class': 'form-select'}),
            'ticket_link': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://...'}),
        }

    def __init__(self, *args, **kwargs):
        organization = kwargs.pop('organization', None)
        super().__init__(*args, **kwargs)
        if organization is not None:
            self.fields['venue'].queryset = Venue.objects.filter(
                organization=organization
            ).order_by('name', 'city')
        else:
            self.fields['venue'].queryset = Venue.objects.none()
        self.fields['description'].required = False
        self.fields['capacity'].required = False
        self.fields['ticket_link'].required = False

        # Add a ChoiceField per dropdown custom field (org-scoped)
        if organization is not None:
            dropdown_fields = CustomField.objects.filter(
                field_type='dropdown', organization=organization
            ).prefetch_related('options').order_by('order', 'name')
        else:
            dropdown_fields = []
        layout_fields = [
            Field('name'),
            Row(
                Column('venue', css_class='form-group col-md-4 mb-0'),
                Column('start_date', css_class='form-group col-md-4 mb-0'),
                Column('start_time', css_class='form-group col-md-4 mb-0'),
            ),
            Row(
                Column('end_date', css_class='form-group col-md-3 mb-0'),
                Column('end_time', css_class='form-group col-md-3 mb-0'),
                Column('capacity', css_class='form-group col-md-3 mb-0'),
                Column('timezone', css_class='form-group col-md-3 mb-0'),
            ),
            Field('description'),
            Field('ticket_link'),
        ]
        for cf in dropdown_fields:
            choices = [('', '---------')] + [
                (opt.id, opt.label) for opt in cf.options.all()
            ]
            field_name = f'custom_field_{cf.id}'
            required = getattr(cf, 'required', False)
            self.fields[field_name] = forms.ChoiceField(
                label=cf.name,
                choices=choices,
                required=required,
                widget=forms.Select(attrs={'class': 'form-select'}),
            )
            default_option_id = getattr(cf, 'default_option_id', None)
            if not self.is_bound and self.instance.pk:
                # Editing: pre-populate with existing value
                from .models import EventCustomFieldValue
                try:
                    existing = EventCustomFieldValue.objects.get(
                        event=self.instance, custom_field=cf
                    )
                    if existing.custom_field_option_id:
                        self.fields[field_name].initial = existing.custom_field_option_id
                except EventCustomFieldValue.DoesNotExist:
                    pass
            elif (
                not self.is_bound
                and (getattr(self.instance, 'pk', None) is None or not self.instance.pk)
                and default_option_id
            ):
                self.fields[field_name].initial = default_option_id
            layout_fields.append(Field(field_name))

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(*layout_fields)

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        start_time = cleaned_data.get('start_time')
        end_date = cleaned_data.get('end_date')
        end_time = cleaned_data.get('end_time')

        if end_time and not end_date:
            self.add_error('end_date', 'End date is required when end time is provided.')

        if end_date and start_date:
            if end_date < start_date:
                self.add_error('end_date', 'End date cannot be before start date.')
            elif end_date == start_date and end_time and start_time:
                if end_time <= start_time:
                    self.add_error('end_time', 'End time must be after start time on the same date.')

        return cleaned_data

    def validate_unique(self):
        try:
            self.instance.validate_unique()
        except forms.ValidationError as e:
            self._update_errors(e)
