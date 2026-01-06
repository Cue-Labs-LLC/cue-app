#!/usr/bin/env python
"""
Script to clear all upload data from the database.
This will delete:
- All UploadedFile records
- All TicketOrder records
- All Ticket records
- All Customer records (since they're created from uploads)
- All Event records (since they're created from uploads)

CSV Format configurations will be preserved.
"""

import os
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ltv_updater.settings')
django.setup()

from tickets.models import UploadedFile, TicketOrder, Ticket, Customer, Event

def clear_all_uploads():
    """Clear all upload-related data."""
    print("Clearing all upload data...")
    print(f"Current counts:")
    print(f"  Uploaded Files: {UploadedFile.objects.count()}")
    print(f"  Ticket Orders: {TicketOrder.objects.count()}")
    print(f"  Tickets: {Ticket.objects.count()}")
    print(f"  Customers: {Customer.objects.count()}")
    print(f"  Events: {Event.objects.count()}")
    
    # Delete in order to respect foreign key constraints
    print("\nDeleting tickets...")
    ticket_count = Ticket.objects.count()
    Ticket.objects.all().delete()
    print(f"  Deleted {ticket_count} tickets")
    
    print("Deleting ticket orders...")
    order_count = TicketOrder.objects.count()
    TicketOrder.objects.all().delete()
    print(f"  Deleted {order_count} ticket orders")
    
    print("Deleting customers...")
    customer_count = Customer.objects.count()
    Customer.objects.all().delete()
    print(f"  Deleted {customer_count} customers")
    
    print("Deleting events...")
    event_count = Event.objects.count()
    Event.objects.all().delete()
    print(f"  Deleted {event_count} events")
    
    print("Deleting uploaded files...")
    upload_count = UploadedFile.objects.count()
    UploadedFile.objects.all().delete()
    print(f"  Deleted {upload_count} uploaded files")
    
    print("\n✅ All upload data cleared!")
    print(f"\nFinal counts:")
    print(f"  Uploaded Files: {UploadedFile.objects.count()}")
    print(f"  Ticket Orders: {TicketOrder.objects.count()}")
    print(f"  Tickets: {Ticket.objects.count()}")
    print(f"  Customers: {Customer.objects.count()}")
    print(f"  Events: {Event.objects.count()}")

if __name__ == '__main__':
    response = input("WARNING: This will delete ALL upload data (orders, tickets, customers, events). Continue? (yes/no): ")
    if response.lower() == 'yes':
        clear_all_uploads()
    else:
        print("Cancelled.")
