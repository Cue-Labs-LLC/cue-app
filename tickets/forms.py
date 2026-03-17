import json
import re as _re
from django import forms
from django.forms import modelformset_factory
from django.contrib.auth.forms import AuthenticationForm
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Row, Column, Submit, Field
from django.forms import inlineformset_factory
from .models import Organization, CSVFormat, Venue, Event, EventTalent, EventExpense, CustomField, IncomeSource, EventIncome, SaleableTicketType, SaleableTicketTypeTier, UserProfile, PromoCode, OrganizerWaitlist


def _normalize_phone(raw: str) -> str:
    """Normalize a phone number string to E.164 format (+1XXXXXXXXXX for US numbers)."""
    phone = raw.strip()
    if not phone.startswith('+'):
        digits = _re.sub(r'\D', '', phone)
        if len(digits) == 10:
            phone = '+1' + digits
        elif len(digits) == 11 and digits[0] == '1':
            phone = '+' + digits
        else:
            phone = '+' + digits  # let regex validation catch invalid formats
    return phone


class OrganizationForm(forms.ModelForm):
    """Form for creating a new organization."""

    class Meta:
        model = Organization
        fields = ['name']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Acme Events'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            Field('name'),
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
    org_role = forms.ChoiceField(
        choices=UserProfile.OrgRole.choices,
        initial=UserProfile.OrgRole.HOST,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            Row(
                Column('email', css_class='form-group col-md-8 mb-0'),
                Column('org_role', css_class='form-group col-md-4 mb-0'),
            ),
            Submit('submit', 'Invite member', css_class='btn btn-primary'),
        )


class AttendeePhoneForm(forms.Form):
    """Form for attendees to enter their phone number (signup or login)."""

    phone_number = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '(555) 000-0000',
            'type': 'tel',
        }),
        help_text='Enter your 10-digit phone number',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            Field('phone_number'),
            Submit('submit', 'Send code', css_class='btn btn-primary w-100'),
        )

    def clean_phone_number(self):
        phone = _normalize_phone(self.cleaned_data['phone_number'])
        if not _re.match(r'^\+[1-9]\d{6,14}$', phone):
            raise forms.ValidationError(
                'Enter a valid phone number (e.g. 5551234567 or +15551234567).'
            )
        return phone


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


class ProfileCompletionForm(forms.Form):
    """Form for new attendees to complete their profile after OTP verification."""

    first_name = forms.CharField(max_length=150, widget=forms.TextInput(attrs={
        'class': 'form-control', 'placeholder': 'First name', 'autofocus': True,
    }))
    last_name = forms.CharField(max_length=150, widget=forms.TextInput(attrs={
        'class': 'form-control', 'placeholder': 'Last name',
    }))
    email = forms.EmailField(widget=forms.EmailInput(attrs={
        'class': 'form-control', 'placeholder': 'Email address',
    }))
    gender = forms.ChoiceField(
        choices=[('', 'Select gender'), ('male', 'Male'), ('female', 'Female'), ('other', 'Other')],
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    marketing_opt_in = forms.BooleanField(
        required=False,
        label='I agree to receive marketing communications',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Row(
                Column('first_name', css_class='col-md-6'),
                Column('last_name', css_class='col-md-6'),
            ),
            Field('email'),
            Field('gender'),
            Field('marketing_opt_in'),
            Submit('submit', 'Complete setup', css_class='btn btn-primary w-100 mt-2'),
        )

    def clean_email(self):
        from django.contrib.auth.models import User
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email


class EmailLoginForm(forms.Form):
    """Form for users to enter their email address for OTP login/signup."""

    email = forms.EmailField(
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'you@example.com',
            'autofocus': True,
        })
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            Field('email'),
            Submit('submit', 'Send code', css_class='btn btn-primary w-100'),
        )

    def clean_email(self):
        return self.cleaned_data['email'].strip().lower()


class EmailProfileCompletionForm(forms.Form):
    """Form for new email-signup attendees to complete their profile."""

    first_name = forms.CharField(max_length=150, widget=forms.TextInput(attrs={
        'class': 'form-control', 'placeholder': 'First name', 'autofocus': True,
    }))
    last_name = forms.CharField(max_length=150, widget=forms.TextInput(attrs={
        'class': 'form-control', 'placeholder': 'Last name',
    }))
    phone_number = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '+1 (555) 000-0000',
            'type': 'tel',
        }),
        help_text='Enter your phone number in international format (e.g. +15551234567)',
        required=False,
    )
    email_display = forms.CharField(
        label='Email address',
        disabled=True,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    gender = forms.ChoiceField(
        choices=[('', 'Select gender'), ('male', 'Male'), ('female', 'Female'), ('other', 'Other')],
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    marketing_opt_in = forms.BooleanField(
        required=False,
        label='I agree to receive marketing communications',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Row(
                Column('first_name', css_class='col-md-6'),
                Column('last_name', css_class='col-md-6'),
            ),
            Field('phone_number'),
            Field('email_display'),
            Field('gender'),
            Field('marketing_opt_in'),
            Submit('submit', 'Complete setup', css_class='btn btn-primary w-100 mt-2'),
        )

    def clean_phone_number(self):
        from .models import UserProfile
        raw = self.cleaned_data.get('phone_number', '').strip()
        if not raw:
            return raw
        phone = _normalize_phone(raw)
        if not _re.match(r'^\+[1-9]\d{6,14}$', phone):
            raise forms.ValidationError(
                'Enter a valid phone number (e.g. 5551234567 or +15551234567).'
            )
        if UserProfile.objects.filter(phone_number=phone).exists():
            raise forms.ValidationError('An account with this phone number already exists.')
        return phone


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
            'city': forms.HiddenInput(),
            'street_address': forms.HiddenInput(),
            'state': forms.HiddenInput(),
            'postal_code': forms.HiddenInput(),
            'country': forms.HiddenInput(),
            'capacity': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'e.g., 500', 'min': '1'}),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['capacity'].required = False
        submit_label = 'Update Venue' if (self.instance and self.instance.pk) else 'Create Venue'
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field('name'),
            Field('capacity'),
            Field('street_address'),
            Field('city'),
            Field('state'),
            Field('postal_code'),
            Field('country'),
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
            'name', 'ticketing_type', 'venue', 'start_date', 'start_time', 'end_date', 'end_time',
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

    def __init__(self, *args, ticketing_type_locked=False, hide_ticket_link=False, **kwargs):
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
        self.fields['start_time'].required = True
        self.fields['end_time'].required = True

        if ticketing_type_locked:
            self.fields['ticketing_type'].widget = forms.HiddenInput()
        else:
            self.fields['ticketing_type'].widget = forms.RadioSelect()

        # Add a ChoiceField per dropdown custom field (org-scoped)
        if organization is not None:
            dropdown_fields = CustomField.objects.filter(
                field_type='dropdown', organization=organization
            ).prefetch_related('options').order_by('order', 'name')
        else:
            dropdown_fields = []
        layout_fields = [
            Field('name'),
            Field('ticketing_type'),
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
            *([Field('ticket_link')] if not hide_ticket_link else []),
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


class SaleableTicketTypeForm(forms.ModelForm):
    """Form for organizers to create/edit a SaleableTicketType."""

    class Meta:
        model = SaleableTicketType
        fields = ['name', 'description', 'price', 'quantity_limit', 'is_active', 'sale_start', 'sale_end', 'order', 'is_password_protected', 'password', 'unlocks_after', 'waitlist_enabled']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. General Admission'}),
            'description': forms.Textarea(attrs={'rows': 2, 'class': 'form-control', 'placeholder': 'Short buyer-facing copy (optional)'}),
            'price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'placeholder': '0.00'}),
            'quantity_limit': forms.NumberInput(attrs={'class': 'form-control', 'min': '1', 'placeholder': 'Leave blank for unlimited'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'sale_start': forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}),
            'sale_end': forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}),
            'order': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
            'is_password_protected': forms.CheckboxInput(attrs={'class': 'form-check-input', 'id': 'id_is_password_protected'}),
            'password': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter password here',
                'autocomplete': 'off',
            }),
            'unlocks_after': forms.Select(attrs={'class': 'form-select'}),
            'waitlist_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['quantity_limit'].required = False
        self.fields['description'].required = False
        self.fields['sale_start'].required = False
        self.fields['sale_end'].required = False
        self.fields['unlocks_after'].required = False
        self.fields['price'].help_text = 'Fallback price when no tiers are configured.'
        submit_label = 'Update Ticket Type' if (self.instance and self.instance.pk) else 'Create Ticket Type'
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field('name'),
            Field('description'),
            Row(
                Column('price', css_class='form-group col-md-4 mb-0'),
                Column('quantity_limit', css_class='form-group col-md-4 mb-0'),
                Column('order', css_class='form-group col-md-4 mb-0'),
            ),
            Row(
                Column('sale_start', css_class='form-group col-md-6 mb-0'),
                Column('sale_end', css_class='form-group col-md-6 mb-0'),
            ),
            Field('is_active'),
            Field('is_password_protected'),
            Field('password'),
            Submit('submit', submit_label, css_class='btn btn-primary'),
        )

    def clean_price(self):
        price = self.cleaned_data.get('price')
        if price is not None and price < 0:
            raise forms.ValidationError('Price must be 0.00 or greater.')
        return price

    def clean(self):
        cleaned_data = super().clean()
        sale_start = cleaned_data.get('sale_start')
        sale_end = cleaned_data.get('sale_end')
        if sale_start and sale_end and sale_end <= sale_start:
            self.add_error('sale_end', 'Sale end must be after sale start.')
        if cleaned_data.get('is_password_protected') and not cleaned_data.get('password'):
            self.add_error('password', 'A password is required when password protection is enabled.')
        return cleaned_data


class SaleableTicketTypeTierForm(forms.ModelForm):
    class Meta:
        model = SaleableTicketTypeTier
        fields = ['name', 'price', 'allotment', 'order']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Early Bird'}),
            'price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0'}),
            'allotment': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
            'order': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
        }


SaleableTicketTypeTierFormSet = inlineformset_factory(
    SaleableTicketType,
    SaleableTicketTypeTier,
    form=SaleableTicketTypeTierForm,
    fields=['name', 'price', 'allotment', 'order'],
    extra=1,
    can_delete=True,
)


class VenueChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        parts = [obj.name]
        address_parts = [p for p in [obj.street_address, obj.city, obj.state] if p]
        if address_parts:
            parts.append(", ".join(address_parts))
        return " — ".join(parts)


class DirectEventForm(forms.ModelForm):
    """Form for creating a direct-ticketing event with venue selection."""
    venue = VenueChoiceField(
        queryset=Venue.objects.none(),
        empty_label="Select a venue",
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    class Meta:
        model = Event
        fields = ['name', 'summary', 'start_date', 'start_time', 'end_date', 'end_time', 'description', 'flyer', 'facebook_pixel_id', 'venue']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Familiar Faces'}),
            'summary': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Short tagline shown on the ticket page (optional)'}),
            'start_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'start_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'}),
            'end_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'end_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'}),
            'description': forms.Textarea(attrs={'rows': 3, 'class': 'form-control', 'placeholder': 'Optional event description'}),
'facebook_pixel_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., 1234567890123456'}),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            self.fields['venue'].queryset = Venue.objects.filter(
                organization=organization
            ).order_by('name', 'city')
        self.fields['summary'].required = False
        self.fields['description'].required = False
        self.fields['start_time'].required = True
        self.fields['end_date'].required = True
        self.fields['end_time'].required = True
        self.fields['flyer'].required = False
        self.fields['facebook_pixel_id'].required = False
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Field('name'),
            Field('summary'),
            Row(
                Column('start_date', css_class='form-group col-md-3 mb-0'),
                Column('start_time', css_class='form-group col-md-3 mb-0'),
                Column('end_date', css_class='form-group col-md-3 mb-0'),
                Column('end_time', css_class='form-group col-md-3 mb-0'),
            ),
            Field('description'),
            Field('venue'),
            Field('facebook_pixel_id'),
        )

    def clean_flyer(self):
        file = self.cleaned_data.get('flyer')
        if not file or not hasattr(file, 'name'):
            return file
        heic_types = {'image/heic', 'image/heif', 'image/heic-sequence', 'image/heif-sequence'}
        is_heic = (
            getattr(file, 'content_type', '') in heic_types
            or file.name.lower().endswith(('.heic', '.heif'))
        )
        if is_heic:
            import io
            from PIL import Image
            from django.core.files.uploadedfile import InMemoryUploadedFile
            img = Image.open(file)
            img = img.convert('RGB')
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=90)
            buf.seek(0)
            name = file.name.rsplit('.', 1)[0] + '.jpg'
            file = InMemoryUploadedFile(buf, 'flyer', name, 'image/jpeg', buf.getbuffer().nbytes, None)
        return file

    def clean_facebook_pixel_id(self):
        import re
        pixel_id = self.cleaned_data.get('facebook_pixel_id', '').strip()
        if pixel_id and not re.fullmatch(r'\d{10,20}', pixel_id):
            raise forms.ValidationError('Facebook Pixel ID must be 10–20 digits.')
        return pixel_id

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        start_time = cleaned_data.get('start_time')
        end_date = cleaned_data.get('end_date')
        end_time = cleaned_data.get('end_time')

        if end_date and start_date:
            if end_date < start_date:
                self.add_error('end_date', 'End date cannot be before start date.')
            elif end_date == start_date and end_time and start_time:
                if end_time <= start_time:
                    self.add_error('end_time', 'End time must be after start time on the same date.')

        return cleaned_data


class SaleableTicketTypeInlineForm(forms.ModelForm):
    class Meta:
        model = SaleableTicketType
        fields = ['name', 'description', 'price', 'quantity_limit', 'order', 'unlocks_after']
        widgets = {
            'name':           forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. General Admission'}),
            'description':    forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Short description (optional)'}),
            'price':          forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'placeholder': '0.00'}),
            'quantity_limit': forms.NumberInput(attrs={'class': 'form-control', 'min': '1', 'placeholder': 'Unlimited'}),
            'order':          forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'style': 'width:65px;'}),
            'unlocks_after':  forms.Select(attrs={'class': 'form-select form-select-sm'}),
        }


DirectTicketTypeFormSet = modelformset_factory(
    SaleableTicketType,
    form=SaleableTicketTypeInlineForm,
    extra=0,
    can_delete=True,
)


class PublicTicketPurchaseForm(forms.Form):
    """
    Dynamically built per-request: one IntegerField per active SaleableTicketType.
    Field names: qty_<uuid_hex> (no hyphens so they're valid HTML names).
    """

    def __init__(self, ticket_types, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ticket_types = ticket_types
        for tt in ticket_types:
            active_tier = tt.get_active_tier()  # tiers already prefetched by caller
            remaining = active_tier.remaining_capacity() if active_tier else tt.remaining_quantity()
            max_val = 10 if remaining is None else min(10, remaining)
            field_name = f'qty_{tt.id.hex}'
            self.fields[field_name] = forms.IntegerField(
                label=tt.name,
                required=False,
                min_value=0,
                max_value=max_val,
                initial=0,
                widget=forms.NumberInput(attrs={
                    'class': 'form-control',
                    'min': '0',
                    'max': str(max_val),
                    'value': '0',
                }),
            )

    def clean(self):
        cleaned_data = super().clean()
        total = sum(
            cleaned_data.get(f'qty_{tt.id.hex}') or 0
            for tt in self._ticket_types
        )
        if total < 1:
            raise forms.ValidationError('Please select at least 1 ticket.')
        return cleaned_data

    def get_line_items(self):
        """Return list of (SaleableTicketType, quantity) for quantities > 0."""
        items = []
        for tt in self._ticket_types:
            qty = self.cleaned_data.get(f'qty_{tt.id.hex}') or 0
            if qty > 0:
                items.append((tt, qty))
        return items


class PromoCodeForm(forms.ModelForm):
    """Organizer form for creating a promo code on an event."""

    class Meta:
        model = PromoCode
        fields = ['code', 'discount_type', 'discount_value', 'max_uses', 'expires_at', 'is_active']
        widgets = {
            'code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. SAVE20'}),
            'discount_type': forms.Select(attrs={'class': 'form-select'}),
            'discount_value': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0.01', 'placeholder': '0.00'}),
            'max_uses': forms.NumberInput(attrs={'class': 'form-control', 'min': '1', 'placeholder': 'Unlimited'}),
            'expires_at': forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.expires_at:
            self.initial['expires_at'] = self.instance.expires_at.strftime('%Y-%m-%dT%H:%M')

    def clean_code(self):
        import re
        code = self.cleaned_data.get('code', '').strip().upper()
        if not re.match(r'^[A-Z0-9\-]+$', code):
            raise forms.ValidationError('Code may only contain letters, numbers, and hyphens.')
        return code

    def clean(self):
        cleaned_data = super().clean()
        discount_type = cleaned_data.get('discount_type')
        discount_value = cleaned_data.get('discount_value')
        if discount_value is not None and discount_value <= 0:
            self.add_error('discount_value', 'Discount value must be greater than 0.')
        if discount_type == PromoCode.PERCENTAGE and discount_value is not None and discount_value > 100:
            self.add_error('discount_value', 'Percentage discount cannot exceed 100.')
        return cleaned_data


class SurveyUploadForm(forms.Form):
    """Upload a CSV survey export from Typeform or similar."""

    csv_file = forms.FileField(
        label='Survey CSV file',
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': '.csv'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_enctype = 'multipart/form-data'
        self.helper.layout = Layout(
            Field('csv_file'),
            Submit('submit', 'Upload Survey', css_class='btn btn-primary mt-2'),
        )

    def clean_csv_file(self):
        f = self.cleaned_data['csv_file']
        if not f.name.lower().endswith('.csv'):
            raise forms.ValidationError('Only .csv files are accepted.')
        if f.size > 5 * 1024 * 1024:
            raise forms.ValidationError('File must be under 5 MB.')
        # Peek at the first line to check column count
        import csv as _csv
        import io
        try:
            f.seek(0)
            first_line = f.readline()
            if isinstance(first_line, bytes):
                first_line = first_line.decode('utf-8-sig')
            reader = _csv.reader(io.StringIO(first_line))
            headers = next(reader, [])
            if len(headers) < 13:
                raise forms.ValidationError(
                    f'Expected at least 13 columns, found {len(headers)}. '
                    'Please check that this is a valid survey export.'
                )
        except forms.ValidationError:
            raise
        except Exception:
            raise forms.ValidationError('Could not read the file. Please check it is a valid CSV.')
        finally:
            f.seek(0)
        return f


class UserProfileForm(forms.Form):
    first_name = forms.CharField(max_length=150, required=False, label='First name')
    last_name  = forms.CharField(max_length=150, required=False, label='Last name')
    phone_number = forms.CharField(
        max_length=20, required=False,
        help_text='International format, e.g. +1 5551234567',
    )
    gender = forms.ChoiceField(
        choices=[('', '— select —')] + list(UserProfile.Gender.choices),
        required=False,
    )
    marketing_opt_in = forms.BooleanField(required=False, label='Marketing emails')

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.helper = FormHelper()
        self.helper.form_tag = False

    def clean_phone_number(self):
        raw = self.cleaned_data.get('phone_number', '').strip()
        if not raw:
            return None
        phone = _normalize_phone(raw)
        if not _re.match(r'^\+[1-9]\d{6,14}$', phone):
            raise forms.ValidationError(
                'Enter a valid phone number (e.g. 5551234567 or +15551234567).'
            )
        qs = UserProfile.objects.filter(phone_number=phone)
        if self.user:
            qs = qs.exclude(user=self.user)
        if qs.exists():
            raise forms.ValidationError('This phone number is already in use.')
        return phone

    def save(self):
        data = self.cleaned_data
        self.user.first_name = data.get('first_name', '')
        self.user.last_name  = data.get('last_name', '')
        self.user.save(update_fields=['first_name', 'last_name'])
        profile = self.user.profile
        profile.phone_number     = data.get('phone_number') or None
        profile.gender           = data.get('gender') or ''
        profile.marketing_opt_in = data.get('marketing_opt_in', False)
        profile.save(update_fields=['phone_number', 'gender', 'marketing_opt_in'])


class WaitlistJoinForm(forms.Form):
    name = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={'placeholder': 'Your name (optional)'}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'placeholder': 'your@email.com'}),
    )


class OrganizerWaitlistForm(forms.ModelForm):
    """Form for prospective organizers to join the beta waitlist."""

    class Meta:
        model = OrganizerWaitlist
        fields = ['name', 'email', 'organization_name', 'instagram_handle']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Your full name'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'your@email.com'}),
            'organization_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Acme Events'}),
            'instagram_handle': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '@yourhandle'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['instagram_handle'].required = False
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            Field('name'),
            Field('email'),
            Field('organization_name'),
            Field('instagram_handle'),
            Submit('submit', 'Join the Waitlist', css_class='btn btn-primary w-100'),
        )
