import asyncio
import os
import json
from datetime import datetime
import websockets

# Server configuration
HOST = '127.0.0.1'
PORT = 8081

# List to keep track of connected clients
clients = set()

async def handle_client(websocket):
    clients.add(websocket)
    try:
        async for message in websocket:
            print(f"Received: {message}")
            await handle_message(message, websocket)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        clients.remove(websocket)

async def handle_message(message, websocket):
    data = json.loads(message)
    action = data.get("action")
    if action == "send_message":
        await log_message(data)
        await broadcast(message, websocket)
    elif action == "get_workspace_list":
        await send_workspace_list(data, websocket)
    elif action == "get_channel_list":
        await send_channel_list(data, websocket)
    elif action == "get_channel_data":
        await send_channel_data(data, websocket)

async def log_message(data):
    workspace = data.get("workspace", "default")
    channel = data.get("channel", "default").strip("#")
    log_dir = os.path.join("logs", workspace, channel)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"{datetime.now().strftime('%Y-%m-%d')}.log")
    with open(log_file, "a", encoding="utf-8") as f:
        log_entry = f"{data['date']}|{data['time']}|{data['sender']}|{data['message']}\n"
        f.write(log_entry)
        
async def send_workspace_list(data, websocket):
    config_file = "workspaces.ini"
    workspaces = {}
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            current_workspace = None
            for line in f:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    current_workspace = line[1:-1]
                    workspaces[current_workspace] = []
                elif current_workspace and line:
                    workspaces[current_workspace].append(line)
    response = json.dumps({
        "date": datetime.now().strftime('%Y-%m-%d'), 
        "time": datetime.now().strftime("%H:%M:%S"), 
        "sender": "Server",
        "action": "workspace_list",
        "message": workspaces
    })
    await websocket.send(response)
    
async def send_channel_list(data, websocket):
    config_file = "workspaces.ini"
    workspace = data.get("workspace", "default")
    channels = []
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            current_workspace = None
            for line in f:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    current_workspace = line[1:-1]
                elif current_workspace == workspace and line:
                    channels.append(line)
    response = json.dumps({
        "date": datetime.now().strftime('%Y-%m-%d'), 
        "time": datetime.now().strftime("%H:%M:%S"), 
        "sender": "Server",
        "action": "channel_list",
        "message": channels
    })
    await websocket.send(response)
    
async def send_channel_data(data, websocket):
    current_time = datetime.now()
    workspace = data.get("workspace", "default")
    channel = data.get("channel", "default").strip("#")
    log_dir = os.path.join("logs", workspace, channel)
    log_file = os.path.join(log_dir, f"{current_time.strftime('%Y-%m-%d')}.log")
    logs = []
    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                date, time, sender, message = line.strip().split("|", 3)
                logs.append({"date": date, 
                             "time": time, 
                             "sender": sender, 
                             "message": message
                             })
    response = json.dumps({
        "date": current_time.strftime('%Y-%m-%d'), 
        "time": current_time.strftime("%H:%M:%S"), 
        "sender": "Server",
        "action": "channel_data",
        "workspace": workspace, 
        "channel": channel, 
        "message": logs
        })
    await websocket.send(response)

async def broadcast(message, sender):
    for client in clients:
        if client != sender:
            try:
                await client.send(message)
            except:
                pass

async def main():
    async with websockets.serve(handle_client, HOST, PORT):
        print(f"Server started on {HOST}:{PORT}")
        await asyncio.Future()  # Run forever

if __name__ == "__main__":
    asyncio.run(main())
