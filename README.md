# Event Ticket Order Upload System

A Django application for uploading and processing CSV files containing event ticket order data, with automatic customer lifetime value (LTV) tracking and flexible CSV format support.

## Features

- 📤 **CSV File Upload**: Upload CSV files with event ticket order data
- 🔧 **Flexible Format Support**: Configure multiple CSV formats with custom column mappings
- 💰 **Lifetime Value Tracking**: Automatic calculation and display of customer LTV
- 📊 **Dashboard**: Overview statistics and recent uploads
- 👥 **Customer Management**: View customers with LTV, order history, and event attendance
- 🔄 **Manual Pricing**: Support for CSV files without price columns
- ⚡ **Performance Optimized**: Chunked processing for files with up to 3000 orders
- 🛡️ **Duplicate Handling**: Automatic detection and skipping of duplicate orders

## Prerequisites

- Python 3.12+ (tested with Python 3.12.1)
- pip (Python package manager)

## Installation

### 1. Clone or Navigate to the Project

```bash
cd enhanced-ltv-updater
```

### 2. Create a Virtual Environment

It's recommended to use a virtual environment to isolate project dependencies:

**On Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

**On macOS/Linux:**
```bash
python -m venv venv
source venv/bin/activate
```

You should see `(venv)` in your terminal prompt, indicating the virtual environment is active.

### 3. Install Dependencies

With the virtual environment activated, upgrade pip (recommended) and install the required packages:

```bash
# Upgrade pip to the latest version
pip install --upgrade pip

# Install project dependencies
pip install -r requirements.txt
```

This will install:
- Django 5.2.4
- django-crispy-forms
- crispy-bootstrap5
- pandas
- python-dateutil
- psycopg2-binary (for PostgreSQL support)

**Note**: Always activate your virtual environment before running any commands. If you close your terminal, you'll need to activate it again.

### 4. Set Up the Database

The application uses SQLite by default for development. Run migrations to create the database schema:

```bash
python manage.py migrate
```

This will create the database file (`db.sqlite3`) and all necessary tables.

### 5. Create a Superuser (Optional but Recommended)

Create an admin user to access the Django admin panel:

```bash
python manage.py createsuperuser
```

Follow the prompts to set a username, email, and password.

## Running the Application

### Start the Development Server

**Important**: Make sure your virtual environment is activated first!

```bash
# Activate virtual environment (if not already active)
# Windows:
venv\Scripts\activate

# macOS/Linux:
source venv/bin/activate

# Then start the server
python manage.py runserver
```

The server will start at `http://127.0.0.1:8000/` (or `http://localhost:8000/`).

### Access the Application

Open your web browser and navigate to:

- **Home/Dashboard**: `http://127.0.0.1:8000/`
- **Upload CSV**: `http://127.0.0.1:8000/upload/`
- **Customers**: `http://127.0.0.1:8000/customers/`
- **CSV Formats**: `http://127.0.0.1:8000/formats/`
- **Admin Panel**: `http://127.0.0.1:8000/admin/`

## Initial Setup

### 1. Create Your First CSV Format

Before uploading CSV files, you need to create at least one CSV format configuration. You can do this in two ways:

#### Option A: Via Web Interface

1. Go to `http://127.0.0.1:8000/formats/create/`
2. Fill in the format details:
   - **Name**: A descriptive name (e.g., "Event Report with Totals")
   - **Description**: Optional description
   - **Is Default**: Check if this should be the default format
   - **Requires Manual Pricing**: Check if your CSV files don't include price/total columns
   - **Column Mapping**: JSON mapping of CSV columns to internal fields (see examples below)

#### Option B: Via Django Admin

1. Go to `http://127.0.0.1:8000/admin/`
2. Log in with your superuser credentials
3. Navigate to **Tickets > CSV Formats**
4. Click **Add CSV Format**
5. Fill in the form and save

### 2. Example CSV Format Configurations

#### Format 1: CSV with Order Totals

For CSV files that include order total columns:

```json
{
  "order_number": ["Order Number", "order_id", "order_number"],
  "order_date": ["Order Date/Time", "date", "order_date"],
  "total_amount": ["Order Total", "total", "amount"],
  "customer_email": ["Email", "customer_email", "email"],
  "customer_name": ["First Name", "Last Name", "name", "customer_name"],
  "customer_phone": ["Phone number", "phone", "customer_phone"],
  "event_name": ["event", "event_name", "show_name"],
  "event_date": ["event_date", "show_date", "event_datetime"],
  "venue": ["venue", "location", "event_venue"],
  "ticket_type": ["Tickets Purchased", "ticket_type", "tier"],
  "quantity": ["# of Tickets", "quantity", "qty"]
}
```

**Settings:**
- Requires Manual Pricing: **No** (unchecked)

#### Format 2: CSV without Prices (Manual Pricing Required)

For CSV files that don't include price or total columns:

```json
{
  "order_number": ["Order Number", "order_id"],
  "order_date": ["Time of purchase", "date", "order_date"],
  "customer_email": ["Email", "customer_email"],
  "customer_name": ["First name", "Last name", "name"],
  "customer_phone": ["Phone number", "phone"],
  "event_name": ["event", "event_name"],
  "event_date": ["event_date", "show_date"],
  "venue": ["venue", "location"],
  "ticket_type": ["Ticket Type", "ticket_type"],
  "quantity": ["Tickets Count", "quantity", "qty"]
}
```

**Settings:**
- Requires Manual Pricing: **Yes** (checked)

Note: No `price` or `total_amount` mappings - these will be entered manually.

### 3. Upload Your First CSV File

1. Go to `http://127.0.0.1:8000/upload/`
2. Select your CSV format from the dropdown
3. Choose your CSV file (max 10MB)
4. Add optional metadata:
   - Description
   - Source (e.g., "Eventbrite", "Manual Entry")
   - File Date
   - Notes
5. Click **Upload CSV**

#### If Manual Pricing is Required

If your format requires manual pricing:

1. After uploading, you'll be redirected to the price entry page
2. Review the ticket types found in your CSV
3. Enter the price for each ticket type
4. Click **Process with Prices**
5. The system will calculate order totals automatically

#### Processing Results

After processing, you'll see:
- Number of successful orders created
- Number of errors (if any)
- Number of duplicate orders skipped
- Detailed error messages (if any)

## Using the Application

### Dashboard

The home page (`/`) displays:
- Total customers
- Total orders
- Total revenue
- Total tickets sold
- Recent uploads with status

### Customer Management

#### View All Customers

Go to `/customers/` to see:
- Customer name, email, phone
- Lifetime Value (LTV) - prominently displayed
- Last order date
- Search and filter capabilities
- Sortable columns

#### View Customer Details

Click on any customer to see:
- Customer profile information
- Lifetime Value (large, prominent display)
- Order statistics (total orders, tickets, average order value)
- Complete order history
- Events attended

### CSV Format Management

Manage your CSV format configurations at `/formats/`:

- **List Formats**: View all configured formats
- **Create Format**: Add a new format configuration
- **Edit Format**: Modify existing formats
- **Set Default**: Mark a format as the default (auto-selected in upload form)
- **Delete Format**: Remove unused formats (only if not in use)

## File Upload Requirements

### Supported File Types
- CSV files only (`.csv` extension)

### File Size Limits
- Maximum file size: 10MB
- Recommended for files with up to 3000 orders

### CSV File Structure

Your CSV files should include columns that match your format configuration. The system will:
- Try multiple column name variations (case-insensitive)
- Handle missing optional fields gracefully
- Validate required fields before processing

## Database

### Development (Default)
- **Database**: SQLite
- **File**: `db.sqlite3` (created automatically)
- No additional configuration needed

### Production
- **Database**: PostgreSQL (recommended)
- Update `settings.py` with your PostgreSQL credentials:

```python
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'your_database_name',
        'USER': 'your_username',
        'PASSWORD': 'your_password',
        'HOST': 'localhost',
        'PORT': '5432',
    }
}
```

## Troubleshooting

### Common Issues

#### 1. "No module named django"
**Solution**: 
- Make sure your virtual environment is activated
- Install dependencies:
```bash
pip install -r requirements.txt
```

#### 2. "Command not found" or "python: command not found"
**Solution**: 
- Make sure your virtual environment is activated
- On Windows, use `python` instead of `python3`
- Verify Python is installed: `python --version`

#### 3. "Table doesn't exist" errors
**Solution**: Run migrations (with venv activated):
```bash
python manage.py migrate
```

#### 4. CSV processing errors
**Check:**
- CSV format configuration matches your file structure
- Required columns are present in your CSV
- Column names match your format mapping (case-insensitive)
- File encoding is UTF-8

#### 5. "File not found" errors
**Solution**: Ensure the `media/uploads/` directory exists:
```bash
mkdir -p media/uploads
```

#### 6. Large file processing takes too long
**Note**: Files with 3000 orders may take 30-90 seconds to process. This is normal. The system processes in chunks to manage memory efficiently.

### Getting Help

1. Check the terminal output for detailed error messages
2. Review the processing results page for specific row errors
3. Verify your CSV format configuration matches your file structure
4. Check Django admin for data validation issues

## Development Commands

**Note**: All commands should be run with your virtual environment activated.

### Run Tests
```bash
python manage.py test
```

### Create Migrations
```bash
python manage.py makemigrations
```

### Apply Migrations
```bash
python manage.py migrate
```

### Django Shell
```bash
python manage.py shell
```

### Collect Static Files (Production)
```bash
python manage.py collectstatic
```

### Deactivate Virtual Environment

When you're done working, you can deactivate the virtual environment:

```bash
deactivate
```

## Project Structure

```
enhanced-ltv-updater/
├── ltv_updater/          # Django project settings
│   ├── settings.py       # Application settings
│   ├── urls.py           # Main URL configuration
│   └── ...
├── tickets/              # Main application
│   ├── models.py         # Database models
│   ├── views.py          # View functions
│   ├── forms.py          # Form definitions
│   ├── services.py       # CSV processing service
│   ├── admin.py          # Django admin configuration
│   ├── urls.py           # App URL routing
│   └── templates/        # HTML templates
│       └── tickets/
├── media/                # Uploaded files (created automatically)
│   └── uploads/
├── db.sqlite3            # SQLite database (created automatically)
├── manage.py             # Django management script
└── requirements.txt      # Python dependencies
```

## Features in Detail

### Customer Lifetime Value (LTV)

- **Automatic Calculation**: LTV is calculated as the sum of all order totals for each customer
- **Real-time Updates**: LTV updates automatically when new orders are processed
- **Display**: Shown prominently in customer list and detail views
- **Sorting**: Customers can be sorted by LTV (highest to lowest)

### Duplicate Order Handling

- Orders with duplicate `order_number` values are automatically skipped
- Skipped duplicates are logged and displayed in processing results
- Processing continues even if duplicates are found
- No errors are raised for duplicates

### Email Normalization

- Customer emails are automatically normalized (lowercase, trimmed)
- Prevents duplicate customers with email variations
- Example: "User@Email.com" and "user@email.com" are treated as the same customer

### Chunked Processing

- Large files are processed in batches of 500 rows
- Prevents memory issues with files containing thousands of orders
- Progress is tracked and displayed
- Each batch is processed in a database transaction for data integrity

How to run stripe webhook
- stripe listen --forward-to localhost:8000/webhooks/stripe/

## License

This project is for personal/internal use.

## Support

For issues or questions, check:
1. The processing results page for specific error messages
2. Django admin for data validation issues
3. Terminal output for detailed error logs

---

**Happy uploading!** 🎉
