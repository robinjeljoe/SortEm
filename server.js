const express = require('express');
const path = require('path');
const session = require('express-session');
const sqlite3 = require('sqlite3').verbose();
const { google } = require('googleapis');
const axios = require('axios');

const app = express();

// --- OAuth2 Configuration ---
const oauth2Client = new google.auth.OAuth2(
    'env.ClientId', // Your Client ID
    'env.ClientSecret', // Your Client Secret
    'env.RedirectUri' // Your Redirect URI
);

const scopes = ['https://mail.google.com/'];

// --- Middleware ---
app.use(express.json());
app.use(express.static('public'));
app.use(session({
    secret: 'your-super-secret-key',
    resave: false,
    saveUninitialized: true,
    cookie: { secure: false }
}));

// --- Authentication ---
const checkAuth = (req, res, next) => {
    if (!req.session.tokens) {
        return res.status(401).json({ error: 'Not authenticated' });
    }
    next();
};

// --- Helper function to get a database connection ---
function getDbConnection(emailAddress) {
    return new Promise((resolve, reject) => {
        const dbName = emailAddress.replace(/[^a-zA-Z0-9]/g, '_') + '.db';
        const db = new sqlite3.Database(dbName, (err) => {
            if (err) {
                console.error('Error opening database:', err.message);
                reject(err);
            } else {
                console.log(`Connected to the ${dbName} database.`);
                db.run(`
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
                `, (err) => {
                    if (err) {
                        console.error('Error creating table:', err.message);
                        reject(err);
                    }
                  db.all("PRAGMA table_info(emails)", (err, rows) => {
                        if (err) {
                            console.error("Error fetching table info:", err);
                            return;
                        }

                        const columns = rows.map(row => row.name);

                        if (!columns.includes('thread_id')) {
                            db.run("ALTER TABLE emails ADD COLUMN thread_id TEXT", err => {
                                if (err) console.error("Error adding thread_id column:", err);
                                else console.log("Added 'thread_id' column.");
                            });
                        }

                        if (!columns.includes('is_read')) {
                            db.run("ALTER TABLE emails ADD COLUMN is_read INTEGER DEFAULT 0", err => {
                                if (err) console.error("Error adding is_read column:", err);
                                else console.log("Added 'is_read' column.");
                            });
                        }
                        if (!columns.includes('date')) {
                            db.run("ALTER TABLE emails ADD COLUMN date INTEGER", err => { //migration
                                if (err) console.error("Error adding date column:", err);
                                else console.log("Added 'date' column.");
                            });
                        }
                        resolve(db); // Resolve with the db object
                    });

                });
            }
        });
    });
}

// --- API Routes (with Pagination) ---



app.get('/api/emails', checkAuth, async (req, res) => {
    try {
        const db = await getDbConnection(req.session.emailAddress);
        const { search, category, sort = 'desc', page = 1, limit = 10 } = req.query; // Default values
        const pageNum = parseInt(page);
        const limitNum = parseInt(limit);
        const offset = (pageNum - 1) * limitNum;
        const sortOrder = (sort === 'asc' || sort === 'desc') ? sort.toUpperCase() : 'DESC';

        let query = "SELECT id, email_id, sender, subject, date, summary, categories FROM emails WHERE 1=1";
        const params = [];

        if (search) {
            const searchTerm = `%${search}%`;
            query += " AND (subject LIKE ? OR sender LIKE ? OR body LIKE ?)";
            params.push(searchTerm, searchTerm, searchTerm);
        }

        if (category) {
            const categoryTerm = `%${category}%`;
            query += " AND categories LIKE ?";
            params.push(categoryTerm);
        }

        query += ` ORDER BY date ${sortOrder} LIMIT ? OFFSET ?`;
        params.push(limitNum, offset);

        db.all(query, params, (err, rows) => {
            if (err) {
                console.error('Error fetching emails:', err.message);
                db.close();
                return res.status(500).json({ error: 'Failed to fetch emails' });
            }

            let countQuery = "SELECT COUNT(*) as total FROM emails WHERE 1=1";
            const countParams = [];
            if (search) {
                countQuery += " AND (subject LIKE ? OR sender LIKE ? OR body LIKE ?)";
                countParams.push(`%${search}%`, `%${search}%`, `%${search}%`);
            }
            if (category) {
                countQuery += " AND categories LIKE ?";
                countParams.push(`%${category}%`);
            }

            db.get(countQuery, countParams, (err, countRow) => {
                if (err) {
                    console.error('Error fetching email count:', err.message);
                    db.close();
                    return res.json({ data: rows, total: 0 }); // Still return data, even if count fails
                }

                const total = countRow.total;
                res.json({ data: rows, total, page: pageNum, limit: limitNum }); // Include pagination info
                db.close();
            });
        });
    } catch (error) {
        console.error("Error in /api/emails:", error);
        res.status(500).json({ error: 'Failed to connect to the database' });
    }
});



app.get('/api/emails/:id', checkAuth, async (req, res) => {
   try {
        const db = await getDbConnection(req.session.emailAddress); // Await the promise
        const emailId = req.params.id;

        db.get("SELECT * FROM emails WHERE id = ?", [emailId], (err, row) => {
            if (err) {
                console.error('Error fetching email detail:', err.message);
                db.close();
                return res.status(500).json({ error: 'Failed to fetch email' });
            }
            if (row) {
                res.json(row);
            } else {
                res.status(404).json({ message: 'Email not found' });
            }
            db.close(); // Close the database connection
        });
    } catch (error) {
        console.error("Error in /api/emails/:id:", error);
        res.status(500).json({ error: 'Failed to connect to the database' });
    }
});


app.get('/api/categories', checkAuth, (req, res) => {
    res.json([
        "Personal", "Family & Friends", "Social Media", "Events & Invitations", "Travel & Bookings",
        "Work & Office", "Meetings & Appointments", "Job Opportunities", "Networking", "Business & Contracts",
        "Finance", "Banking & Transactions", "Bills & Invoices", "Taxes", "Legal & Compliance",
        "Technology", "Software Updates", "Security Alerts", "Online Services", "AI & Automation",
        "Education", "Online Learning", "Courses & Certifications", "Research & Academia",
        "Health", "Medical & Insurance", "Fitness & Lifestyle",
        "Shopping & E-commerce", "Orders & Deliveries", "Subscriptions & Memberships",
        "News & Updates", "Entertainment", "Music & Streaming Services", "Books & Publications",
        "Announcements", "Ads & Promotions", "Discounts & Offers", "Surveys & Feedback"
    ]);
});

// --- OAuth Routes ---

app.get('/auth/google', (req, res) => {
    const authUrl = oauth2Client.generateAuthUrl({
        access_type: 'offline',
        scope: scopes,
        prompt: 'consent'
    });
    res.redirect(authUrl);
});

app.get('/auth/google/callback', async (req, res) => {
    const { code } = req.query;
    try {
        const { tokens } = await oauth2Client.getToken(code);
        req.session.tokens = tokens;

        // Get user's email
        oauth2Client.setCredentials(tokens);
        const gmail = google.gmail({ version: 'v1', auth: oauth2Client });
        const profile = await gmail.users.getProfile({ userId: 'me' });
        const emailAddress = profile.data.emailAddress;

        req.session.emailAddress = emailAddress;
        await sendDataToFetchScript({ tokens, emailAddress });
        res.redirect('/emails.html');

    } catch (error) {
        console.error('Error during authentication:', error);
        res.redirect('/login.html');
    }
});

// --- Logout (with Token Deletion) ---
app.get('/auth/logout', async (req, res) => {
    try {
        // Clear tokens in fetch.py
        await axios.post('http://localhost:5001/clear_tokens');
        console.log('Tokens cleared in fetch script.');
    } catch (error) {
        console.error('Error clearing tokens in fetch script:', error);
        // Even if clearing tokens fails, proceed with logout
    }

    req.session.destroy(err => {
        if (err) {
            console.error("Error destroying session:", err);
            return res.status(500).send("Could not log out.");
        }
        res.redirect('/login.html');
    });
});


// --- Serve HTML Pages ---

app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// --- Helper Function to Send Data to fetch.py ---
async function sendDataToFetchScript(data) {
    try {
        const response = await axios.post('http://localhost:5001/update_data', data);
        console.log('Data sent to fetch script:', response.data);
    } catch (error) {
        console.error('Error sending data to fetch script:', error);
    }
}

// --- Start Server ---
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log(`Server running on port ${PORT}`);
});