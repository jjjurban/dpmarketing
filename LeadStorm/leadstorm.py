import json
import requests
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from openai import OpenAI
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser
import logging
import time
from typing import List, Dict
import facebook_scraper as fb

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load configuration from config.json
try:
    with open('config.json', 'r') as config_file:
        CONFIG = json.load(config_file)
    HUNTER_KEY = CONFIG['hunter_key']
    OPENAI_KEY = CONFIG['openai_key']
    GOOGLE_CREDS = CONFIG['google_creds']
except FileNotFoundError:
    logging.error("config.json not found. Please create it with your API keys.")
    raise
except KeyError as e:
    logging.error(f"Missing key in config.json: {str(e)}")
    raise

LEADS_PER_RUN = 10
FB_GROUP = "general"  # Use a specific public group ID (e.g., "123456789") if known

def scrape_public_fb_leads(target_audience: str, progress_bar: ttk.Progressbar, root: tk.Tk) -> List[Dict]:
    """Scrape public FB posts without login."""
    try:
        logging.info(f"Scraping public Facebook for '{target_audience}'...")
        progress_bar['value'] = 10
        root.update_idletasks()
        posts = fb.get_posts(group=FB_GROUP, pages=5)  # No credentials, public data only
        leads = []
        for post in posts:
            if post['text'] and target_audience.lower() in post['text'].lower():
                leads.append({
                    "username": post['username'] or "unknown",
                    "name": post.get('name', 'Unknown User'),
                    "post": post['text'],
                    "source": "Facebook"
                })
                logging.info(f"Found lead: {post['text'][:50]}...")
            if len(leads) >= LEADS_PER_RUN * 2:
                break
        progress_bar['value'] = 25
        root.update_idletasks()
        logging.info(f"Scraped {len(leads)} raw leads from FB.")
        return leads
    except Exception as e:
        logging.error(f"FB scrape failed: {str(e)}")
        return []

def qualify_leads(leads: List[Dict], progress_bar: ttk.Progressbar, root: tk.Tk) -> List[Dict]:
    """Qualify leads with OpenAI, with robust error handling."""
    try:
        logging.info("Qualifying leads with OpenAI...")
        client = OpenAI(api_key=OPENAI_KEY)
        qualified = []
        step_increment = 25 / max(1, len(leads))
        current_progress = 25
        for lead in leads:
            prompt = f"Score this lead (0-10) for fit as a potential customer based on: Post: {lead['post']}"
            try:
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=10
                )
                raw_score = response.choices[0].message.content.strip()
                logging.info(f"Raw OpenAI score for '{lead['post'][:50]}...': {raw_score}")
                score = int(raw_score) if raw_score.isdigit() else 0  # Fallback to 0 if not a number
            except Exception as e:
                logging.error(f"OpenAI call failed for lead '{lead['post'][:50]}...': {str(e)}")
                score = 0  # Fallback score on error
            if score >= 5:
                lead['score'] = score
                qualified.append(lead)
            current_progress += step_increment
            progress_bar['value'] = min(current_progress, 50)
            root.update_idletasks()
            time.sleep(1)
            if len(qualified) >= LEADS_PER_RUN:
                break
        progress_bar['value'] = 50
        root.update_idletasks()
        logging.info(f"Qualified {len(qualified)} leads.")
        return qualified
    except Exception as e:
        logging.error(f"OpenAI qualification failed entirely: {str(e)}")
        return []

def enrich_leads(leads: List[Dict], progress_bar: ttk.Progressbar, root: tk.Tk) -> List[Dict]:
    """Enrich leads with email addresses using Hunter.io."""
    try:
        logging.info("Enriching leads with Hunter.io...")
        step_increment = 25 / max(1, len(leads))
        current_progress = 50
        for lead in leads:
            url = f"https://api.hunter.io/v2/email-finder?full_name={lead['name']}&api_key={HUNTER_KEY}"
            response = requests.get(url, timeout=5).json()
            lead['email'] = response.get('data', {}).get('email', 'N/A')
            lead['why_fit'] = f"Post: {lead['post'][:50]}..."
            current_progress += step_increment
            progress_bar['value'] = min(current_progress, 75)
            root.update_idletasks()
            time.sleep(1)
        progress_bar['value'] = 75
        root.update_idletasks()
        logging.info(f"Enriched {len(leads)} leads.")
        return leads
    except Exception as e:
        logging.error(f"Hunter.io enrichment failed: {str(e)}")
        return leads

def upload_to_sheets(leads: List[Dict], progress_bar: ttk.Progressbar, root: tk.Tk) -> str:
    """Upload leads to a Google Sheet and return its URL."""
    try:
        logging.info("Uploading leads to Google Sheets...")
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keydict(GOOGLE_CREDS, scope)
        client = gspread.authorize(creds)
        sheet_name = f"LeadStorm_{int(time.time())}"
        sheet = client.create(sheet_name).sheet1
        sheet.spreadsheet.share(None, perm_type='anyone', role='writer')
        
        df = pd.DataFrame(leads)
        if df.empty:
            df = pd.DataFrame([{"name": "No leads found", "email": "N/A", "why_fit": "Try a different audience"}])
        sheet.update([df.columns.values.tolist()] + df.values.tolist())
        progress_bar['value'] = 100
        root.update_idletasks()
        logging.info(f"Leads uploaded to {sheet_name}")
        return f"https://docs.google.com/spreadsheets/d/{sheet.spreadsheet.id}"
    except Exception as e:
        logging.error(f"Google Sheets upload failed: {str(e)}")
        raise

def run_leadstorm():
    """Main function with a loading bar."""
    root = tk.Tk()
    root.title("LeadStorm")
    root.geometry("300x150")
    root.resizable(False, False)

    tk.Label(root, text="Who do you sell to? (e.g., 'small business owners')").pack(pady=10)
    audience_entry = tk.Entry(root, width=30)
    audience_entry.pack(pady=5)

    progress_bar = ttk.Progressbar(root, maximum=100, length=250, mode='determinate')
    progress_bar.pack(pady=10)

    def start_process():
        audience = audience_entry.get().strip()
        if not audience:
            messagebox.showerror("LeadStorm", "Enter an audience first!")
            return
        
        logging.info(f"Starting LeadStorm for audience: {audience}")
        start_button.config(state="disabled")
        
        raw_leads = scrape_public_fb_leads(audience, progress_bar, root)
        if not raw_leads:
            messagebox.showwarning("LeadStorm", "No leads found on public FB. Try a different audience.")
            root.destroy()
            return
        
        qualified_leads = qualify_leads(raw_leads, progress_bar, root)
        if not qualified_leads:
            messagebox.showwarning("LeadStorm", "No leads scored high enough. Try a broader audience.")
            root.destroy()
            return
        
        enriched_leads = enrich_leads(qualified_leads, progress_bar, root)
        sheet_url = upload_to_sheets(enriched_leads, progress_bar, root)
        
        messagebox.showinfo("LeadStorm", f"Done! Found {len(enriched_leads)} leads: {sheet_url}")
        webbrowser.open(sheet_url)
        root.destroy()

    start_button = tk.Button(root, text="Get Leads", command=start_process)
    start_button.pack(pady=10)

    root.mainloop()

if __name__ == "__main__":
    try:
        run_leadstorm()
    except Exception as e:
        logging.error(f"LeadStorm crashed: {str(e)}")
        messagebox.showerror("LeadStorm", "Something went wrong. Check your setup and try again.")