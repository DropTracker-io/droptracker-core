
## Libraries
from quart import Quart, request, jsonify, abort
from quart_jwt_extended import JWTManager, jwt_required, create_access_token
from quart_rate_limiter import RateLimiter, rate_limit
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
from sqlalchemy import desc, func, create_engine, inspect
from sqlalchemy.orm import sessionmaker, scoped_session
from contextlib import contextmanager
import time
from collections import defaultdict, deque
import threading
import asyncio
import pymysql

## Core package dependencies
from data import submissions
from data.submissions import ca_processor, clog_processor, drop_processor, pb_processor
from db.models import Session, session, Player, Group, CollectionLogEntry, PersonalBestEntry, PlayerPet, ItemList, NpcList



## API Packages
from api.services.metrics import MetricsTracker

from utils.download import download_image, download_player_image

# Load environment variables
load_dotenv()

# Initialize Quart app
app = Quart(__name__)
rate_limiter = RateLimiter(app)

# Configure app
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_TOKEN_KEY")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(days=1)
jwt = JWTManager(app)

# Create a single global engine and session factory
db_url = f"mysql+pymysql://root:{os.getenv('DB_PASS')}@localhost:3306/data"
engine = create_engine(
    db_url,
    pool_recycle=3600,  # Recycle connections after 1 hour
    pool_pre_ping=True, # Check connection validity before using
    pool_size=10,       # Maximum number of connections
    max_overflow=20     # Allow up to 20 connections beyond pool_size
)
Session = scoped_session(sessionmaker(bind=engine))

# Simple function to get a fresh session
def get_db_session():
    return Session()

# Function to reset all connections
def reset_db_connections():
    Session.remove()
    engine.dispose()
    print("Database connections reset")


# Initialize metrics tracker
metrics = MetricsTracker()


@app.route("/submit", methods=["POST"])
@rate_limit(limit=10,period=timedelta(seconds=1))
async def submit_data():
    return await webhook_data()
    
@app.errorhandler(404)
async def not_found(e):
    return jsonify({"error": "Resource not found"}), 404

@app.errorhandler(500) 
async def server_error(e):
    return jsonify({"error": "Internal server error"}), 500

@app.route("/player", methods=["GET"])
@rate_limit(limit=5,period=timedelta(seconds=10))
async def get_player():
    """Get player data"""
    player_name = request.args.get("player_name")
    if not player_name:
        return jsonify({"error": "Player name is required"}), 400
    player = session.query(Player).filter(Player.player_name == player_name).first()
    if not player:
        return jsonify({"error": "Player not found"}), 404
    return jsonify({"player": player.to_dict()})

@app.route("/metrics", methods=["GET"])
async def get_metrics():
    auth_key = request.headers.get("Authorization")
    if auth_key != os.getenv("BACKEND_ACP_TOKEN"):
        return jsonify({"error": "Unauthorized"}), 401
    """Get current metrics"""
    return jsonify(metrics.get_stats())

@app.route("/latest_news", methods=["GET"])
async def get_latest_news():
    """Get the latest news"""
    return "You have the api enabled & we've connected properly."

@app.route("/webhook", methods=["POST"])
@rate_limit(limit=10,period=timedelta(seconds=1))
async def webhook_data():
    """
    Handle Discord webhook-style messages and convert them to the standard format
    for processing by the existing processors.
    """
    success = False
    request_type = "webhook"
    
    try:
        # Debug the raw request to see what's coming in
        content_type = request.headers.get('Content-Type', '')
        print("Request content type:", content_type)
        
        # Check if this is a multipart request
        if 'multipart/form-data' in content_type:
            try:
                # Get the boundary from the content type
                boundary = None
                for part in content_type.split(';'):
                    part = part.strip()
                    if part.startswith('boundary='):
                        boundary = part[9:].strip('"')
                        break
                
                print(f"Found boundary: {boundary}")
                
                # Get the raw request data
                body = await request.body
                body_str = body.decode('latin1')  # Use latin1 to handle binary data
                
                # Print the first 200 chars to see the structure
                print(f"Request body preview: {body_str[:200]}...")
                
                # Look for the file part in the raw body
                file_header = f'Content-Disposition: form-data; name="file"; filename="image.jpeg"'
                
                # Process the form data normally
                form = await request.form
                print(f"Form keys: {list(form.keys())}")
                
                payload_json = form.get('payload_json')
                if not payload_json:
                    return jsonify({"error": "No payload_json found in form data"}), 400
                
                # Parse the JSON payload
                import json
                webhook_data = json.loads(payload_json)
                
                if webhook_data is None:
                    return jsonify({"error": "Invalid JSON in payload_json"}), 400
                
                print("Parsed webhook data:", webhook_data)
                
                # Try to get the file directly from the request files
                files = await request.files
                print(f"Request files: {files}")
                
                # Handle image file if present
                image_file = None
                if 'file' in files:
                    image_file = files['file']
                    print(f"Received image file: {image_file.filename}, "
                          f"content_type: {image_file.content_type}")
                
                # Extract data from Discord webhook format
                processed_data = await process_webhook_data(webhook_data)
                
                if not processed_data:
                    return jsonify({"error": "Could not process webhook data"}), 400
                
                # Add image data to processed_data if available
                submission_type = processed_data.get("type")
                processed_data["downloaded"] = False
                
                if image_file:
                    print("Got image file in form data")
                    processed_data["has_image"] = True
                    with Session() as session:
                        player = session.query(Player).filter(Player.player_name == processed_data.get("player", None)).first()
                        if player:
                            file_path = await download_image(submission_type, player, image_file, processed_data)
                            processed_data["image_url"] = file_path
                            processed_data["downloaded"] = True
                
                # Create a fresh database connection for each request
                session = get_db_session()
                try:
                    match (submission_type):
                        case "drop" | "other"| "npc":
                            print("Sent to drop processor")
                            await submissions.drop_processor(processed_data, external_session=session)
                        case "collection_log":
                            print("Sent to clog processor")
                            await submissions.clog_processor(processed_data, external_session=session)
                        case "personal_best":
                            print("Sent to pb processor")
                            await submissions.pb_processor(processed_data, external_session=session)
                        case "combat_achievement":
                            print("Sent to ca processor")
                            await submissions.ca_processor(processed_data, external_session=session)
                        case _:
                            return jsonify({"error": f"Unknown submission type: {submission_type}"}), 400
                except Exception as processor_error:
                    print(f"Processor error: {processor_error}")
                    # Roll back on error
                    session.rollback()
                    return jsonify({"error": f"Error processing data: {str(processor_error)}"}), 500
                finally:
                    # Always close the session
                    reset_db_connections()
                
                success = True
                return jsonify({"message": "Webhook data processed successfully"}), 200
                
            except Exception as e:
                print(f"Error processing multipart request: {e}")
                return jsonify({"error": f"Error processing request: {str(e)}"}), 400
        else:
            # Handle non-multipart requests (e.g., JSON)
            try:
                data = await request.get_json()
                # Process JSON data...
                return jsonify({"message": "JSON data processed"}), 200
            except Exception as e:
                print(f"Error processing JSON request: {e}")
                return jsonify({"error": f"Error processing request: {str(e)}"}), 400
    except Exception as e:
        print("Webhook Exception: ", e)
        # Force cleanup of any lingering sessions
        reset_db_connections()
        return jsonify({"error": str(e)}), 500
    finally:
        # Record metrics regardless of success/failure
        metrics.record_request(request_type, success)

async def process_webhook_data(webhook_data):
    """Process webhook data from Discord format to standard format"""
    try:
        # Extract the content and embeds from the webhook data
        embeds = webhook_data.get("embeds", [])
        
        if not embeds:
            print("No embeds found in webhook data")
            return None
        
        # Process the first embed
        embed = embeds[0]
        
        # Extract fields from the embed and create the data structure directly
        processed_data = {
            field["name"]: field["value"] for field in embed.get("fields", [])
        }
        
        # Add timestamp
        processed_data["timestamp"] = datetime.now().isoformat()
        
        return processed_data
    except Exception as e:
        print(f"Error processing webhook data: {e}")
        return None

async def save_image(file_data, processed_data):
    """Save uploaded image to disk with a unique filename"""
    try:
        import os
        import uuid
        
        # Create directory if it doesn't exist
        upload_dir = os.path.join(os.getcwd(), "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        
        # Generate unique filename
        filename = f"{uuid.uuid4()}.jpg"
        filepath = os.path.join(upload_dir, filename)
        
        # Save the file
        await file_data.save(filepath)
        
        # Add the filepath to the processed data
        processed_data["image_path"] = filepath
        
        print(f"Saved image to {filepath}")
        return filepath
    except Exception as e:
        print(f"Error saving image: {e}")
        return None

async def get_item_id_by_name(item_name):
    """Get item ID from name with robust connection handling"""
    for attempt in range(3):  # Try up to 3 times
        session = None
        try:
            session = get_db_session()
            item = session.query(ItemList.item_id).filter(
                ItemList.item_name.ilike(f"%{item_name}%")
            ).first()
            result = item[0] if item else 0
            session.close()
            return result
        except Exception as e:
            print(f"Database error in get_item_id_by_name (attempt {attempt+1}/3): {e}")
            if session:
                try:
                    session.rollback()
                    session.close()
                except:
                    pass
            
            if attempt == 2:  # Last attempt
                print(f"Failed to get item ID for '{item_name}' after 3 attempts")
                return 0
            await asyncio.sleep(0.5)  # Wait before retrying

async def get_npc_id_by_name(npc_name):
    """Get NPC ID from name with robust connection handling"""
    for attempt in range(3):  # Try up to 3 times
        session = None
        try:
            session = get_db_session()
            npc = session.query(NpcList.npc_id).filter(
                NpcList.npc_name.ilike(f"%{npc_name}%")
            ).first()
            result = npc[0] if npc else 0
            session.close()
            return result
        except Exception as e:
            print(f"Database error in get_npc_id_by_name (attempt {attempt+1}/3): {e}")
            if session:
                try:
                    session.rollback()
                    session.close()
                except:
                    pass
                
            if attempt == 2:  # Last attempt
                print(f"Failed to get NPC ID for '{npc_name}' after 3 attempts")
                return 0
            await asyncio.sleep(0.5)  # Wait before retrying

def convert_time_to_ms(time_str):
    """Convert time string (e.g. '1:23.45') to milliseconds"""
    try:
        if ":" in time_str:
            parts = time_str.split(":")
            if len(parts) == 2:
                minutes, seconds = parts
                return (int(minutes) * 60 + float(seconds)) * 1000
            elif len(parts) == 3:
                hours, minutes, seconds = parts
                return (int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000
        return int(float(time_str) * 1000)
    except:
        return 0


if __name__ == "__main__":
    import socket
    import time
    
    # Try to kill any process using the port
    import os
    os.system(f"kill $(lsof -t -i:31323) 2>/dev/null || true")
    
    # Wait a moment for the port to be released
    time.sleep(1)
    
    # Check if port is available before starting
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 31323))
        sock.close()
        print("Port 31323 is available, starting server...")
        app.run(host="127.0.0.1", port=31323)
    except OSError as e:
        print(f"Port 31323 is still in use. Error: {e}")
        print("Please manually check for processes using this port with:")
        print("sudo lsof -i:31323")
        print("Then kill the process with:")
        print("kill -9 <PID>")