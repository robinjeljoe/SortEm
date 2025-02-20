import imaplib
import email
from sched import scheduler
import sqlite3
import time
import os
from pydantic import BaseModel
from transformers import pipeline
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
import email.utils
from flask import Flask, request, jsonify
from flask_cors import CORS
import re  # Import the regular expression module

# --- Flask App for Receiving Data ---
app = Flask(__name__)
CORS(app)

# Global variables to store data
current_tokens = None
current_email_address = None

summarizer = pipeline("summarization", model="facebook/bart-large-cnn")

classifier = pipeline("zero-shot-classification", model="facebook/bart-large-mnli")

@app.route('/update_data', methods=['POST'])
def update_data():
    global current_tokens, current_email_address
    data = request.json
    current_tokens = data.get('tokens')
    current_email_address = data.get('emailAddress')
    print("Received tokens:", current_tokens)
    print("Received email address:", current_email_address)
    if current_email_address: # Check if email address received
        setup_database() #call set up database here.
    else:
        print("Error: Email address not received from Node.js server.") # Handling the error
    return jsonify({'message': 'Data received'}), 200

# Add this route to clear tokens
@app.route('/clear_tokens', methods=['POST'])
def clear_tokens():
    global current_tokens, current_email_address
    current_tokens = None
    current_email_address = None
    print("Tokens and email address cleared.")
    return jsonify({'message': 'Tokens cleared'}), 200

# --- Database Functions (Modified for dynamic DB name) ---

def get_db_connection():
    if not current_email_address:
        raise Exception("Email address not set!")
    # Use re.sub() for regular expression substitution
    db_name = re.sub(r'[^a-zA-Z0-9]', '_', current_email_address) + '.db'
    conn = sqlite3.connect(db_name)
    conn.row_factory = sqlite3.Row
    return conn

def setup_database():
    try:
        conn = get_db_connection()  # Use the new connection function
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id TEXT UNIQUE,
                thread_id TEXT,
                sender TEXT,
                subject TEXT,
                body TEXT,
                date INTEGER,
                summary TEXT,
                categories TEXT,
                is_read INTEGER DEFAULT 0
            )
        ''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS seen_uids (uid INTEGER PRIMARY KEY)''')
        cursor.execute("PRAGMA table_info(emails)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'thread_id' not in columns:
            cursor.execute("ALTER TABLE emails ADD COLUMN thread_id TEXT")
            print("Added 'thread_id' column.")
        if 'is_read' not in columns:
            cursor.execute("ALTER TABLE emails ADD COLUMN is_read INTEGER DEFAULT 0")
            print("Added 'is_read' column.")
        if 'date' not in columns:
            cursor.execute("ALTER TABLE emails ADD COLUMN date INTEGER")
            print("Added 'date' column (INTEGER).")
        conn.commit()
        conn.close()
    except Exception as e: #Catching the errors
        print(f"An error occurred during database setup: {e}")

def save_email_to_db(email_id, thread_id, sender, subject, body, date, summary, categories, is_read=0):
    conn = get_db_connection()  # Use the new connection function
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO emails (email_id, thread_id, sender, subject, body, date, summary, categories, is_read)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email_id) DO UPDATE SET
                thread_id=excluded.thread_id,
                sender=excluded.sender,
                subject=excluded.subject,
                body=excluded.body,
                date=excluded.date,
                summary=excluded.summary,
                categories=excluded.categories,
                is_read=excluded.is_read
        ''', (email_id, thread_id, sender, subject, body, date, summary, categories, is_read))
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database error: {e}")
    finally:
        conn.close()

def mark_email_as_read_db(email_id):
    conn = get_db_connection() # Use the new connection function
    cursor = conn.cursor()
    cursor.execute("UPDATE emails SET is_read = 1 WHERE email_id = ?", (email_id,))
    conn.commit()
    conn.close()

def uid_is_seen(uid):
    conn = get_db_connection()  # Use the new connection function
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM seen_uids WHERE uid = ?", (uid,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def add_uid_to_seen(uid):
    conn = get_db_connection()  # Use the new connection function
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO seen_uids (uid) VALUES (?)", (uid,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()
# --- IMAP Functions ---

def generate_oauth2_string(email_address, access_token):
    auth_string = f"user={email_address}\1auth=Bearer {access_token}\1\1"
    return auth_string

def connect_to_mailbox():
    global current_tokens, current_email_address
    if not current_tokens or 'access_token' not in current_tokens or not current_email_address:
        print("Missing tokens or email address. Waiting...")
        return None

    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        auth_string = generate_oauth2_string(current_email_address, current_tokens['access_token'])
        mail.authenticate('XOAUTH2', lambda x: auth_string.encode('utf-8'))
        mail.select('inbox')
        return mail
    except Exception as e:
        print(f"Error connecting to mailbox: {e}")
        return None
# --- (fetch_email_uids, process_email, summarize_email_body, categorize_email remain the same) ---
def fetch_email_uids(mail):
    """Fetches ALL email UIDs, regardless of read/unread status."""
    try:
        # Fetch ALL emails
        status, messages = mail.uid('search', None, 'ALL')

        if status != 'OK':
            print("No messages found or error during search.")
            return []

        email_uids = messages[0].split()
        if not email_uids:
            print("No emails found.")
            return []

        print(f"Found {len(email_uids)} emails.")
        return email_uids
    except Exception as e:
        print(f"Error fetching email UIDs: {e}")
        return []

def process_email(mail, email_uid_bytes):
    """Processes a single email, fetches details, and saves it."""
    email_uid = email_uid_bytes.decode('utf-8')

    if uid_is_seen(email_uid):
        print(f"Email with UID {email_uid} already processed. Skipping.")
        return  # Skip if already seen

    try:
        status, msg_data = mail.uid('fetch', email_uid_bytes, '(RFC822)')
        if status != 'OK':
            print(f"Error fetching email with UID {email_uid}")
            return

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        sender = str(msg.get("From", "N/A"))
        subject = str(msg.get("Subject", "No Subject"))
        email_date_str = str(msg.get("Date", "N/A"))
        thread_id = msg.get("X-GM-THRID", "N/A")

        # Parse the date string into a datetime object, handling timezones.
        parsed_date = email.utils.parsedate_to_datetime(email_date_str)
        # Convert the datetime object to a Unix timestamp (seconds since epoch).
        timestamp = int(parsed_date.timestamp())


        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                if content_type == "text/plain" and "attachment" not in content_disposition:
                    try:
                        body = part.get_payload(decode=True).decode()
                    except:
                        body = "Could not decode."
                elif content_type == "text/html" and "attachment" not in content_disposition:
                    try:
                        body_html = part.get_payload(decode=True).decode()
                        soup = BeautifulSoup(body_html, 'html.parser')
                        body = soup.get_text(separator='\n', strip=True)
                    except:
                        body = "Could not decode."
                if body:
                    break
        else:
            content_type = msg.get_content_type()
            if content_type == "text/plain":
                try:
                    body = msg.get_payload(decode=True).decode()
                except:
                    body = "Could not decode."
            elif content_type == "text/html":
                try:
                    body_html = msg.get_payload(decode=True).decode()
                    soup = BeautifulSoup(body_html, 'html.parser')
                    body = soup.get_text(separator='\n', strip=True)
                except:
                    body = "Could not decode."
            else:
                body = "Empty or unreadable."

        summary = summarize_email_body(body)
        categories = categorize_email(sender, subject, body)

        # Determine read status based on IMAP flags
        status, flags_data = mail.uid('fetch', email_uid_bytes, '(FLAGS)')
        is_read = 1 if b'\\Seen' in flags_data[0] else 0

        save_email_to_db(email_uid, thread_id, sender, subject, body, timestamp, summary, categories, is_read) #save the timestamp
        add_uid_to_seen(email_uid)  # Add to seen UIDs *after* saving
        print(f"Processed and saved email with UID: {email_uid}")

    except Exception as e:
        print(f"Error processing email with UID {email_uid}: {e}")

def summarize_email_body(email_body, summary_length=3):
    
    max_input_length = 1024  # Adjust based on the model's max sequence length
    truncated_body = email_body[:max_input_length]
    summary = summarizer(truncated_body, max_length=50, min_length=15, do_sample=False)[0]['summary_text']
    return summary

def categorize_email(sender, subject, body):
    categories = []
    candidate_labels = [
    "Personal", "Family & Friends", "Social Media", "Events & Invitations", "Travel & Bookings",
    "Work & Office", "Meetings & Appointments", "Job Opportunities", "Networking", "Business & Contracts",
    "Finance", "Banking & Transactions", "Bills & Invoices", "Taxes", "Legal & Compliance",
    "Technology", "Software Updates", "Security Alerts", "Online Services", "AI & Automation",
    "Education", "Online Learning", "Courses & Certifications", "Research & Academia",
    "Health", "Medical & Insurance", "Fitness & Lifestyle",
    "Shopping & E-commerce", "Orders & Deliveries", "Subscriptions & Memberships",
    "News & Updates", "Entertainment", "Music & Streaming Services", "Books & Publications",
    "Announcements", "Ads & Promotions", "Discounts & Offers", "Surveys & Feedback"
]
    tags = classifier(body, candidate_labels)
    refined_tags = [label for label, score in zip(tags['labels'], tags['scores']) if score >= 0.05]
    return ", ".join(refined_tags)

def fetch_and_process():
    print(f"Fetching emails at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    mail = connect_to_mailbox()
    if not mail:
        return

    try:
        email_uids = fetch_email_uids(mail)
        for email_uid_bytes in email_uids:
            process_email(mail, email_uid_bytes)
    finally:
        try:
            mail.close()
            mail.logout()
        except:
            pass

def start_scheduler():
    REFRESH_INTERVAL = 12
    scheduler = BackgroundScheduler()
    scheduler.add_job(fetch_and_process, 'interval', seconds=REFRESH_INTERVAL)
    scheduler.start()
    print(f"Scheduler started. Fetching every {REFRESH_INTERVAL} seconds.")

# --- Main Execution ---
if __name__ == "__main__":
    # setup_database() No longer called here, only after getting the email
    start_scheduler()

    # Start Flask app in a separate thread
    import threading
    flask_thread = threading.Thread(target=lambda: app.run(debug=False, port=5001, use_reloader=False))
    flask_thread.daemon = True
    flask_thread.start()

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print("Scheduler stopped.")