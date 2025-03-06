import asyncio
import os
import json
from datetime import datetime
import websockets
from pymongo import MongoClient
import motor.motor_asyncio
import bson
from bson.objectid import ObjectId
import logging

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('slack-server')

# Server configuration
HOST = '127.0.0.1'
PORT = 8081

# MongoDB 설정
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "slack_clone"

# MongoDB 클라이언트 설정
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = mongo_client[DB_NAME]

# 컬렉션 초기화
workspaces_collection = db['workspaces']
channels_collection = db['channels']
messages_collection = db['messages']
users_collection = db['users']

# 연결된 클라이언트 추적
clients = set()
client_info = {}  # {websocket: {"user_id": id, "workspace": workspace}}

# 데이터베이스 초기화 함수
async def initialize_db():
    """MongoDB 데이터베이스 초기화 - 기본 워크스페이스 및 채널 설정"""
    # 워크스페이스가 없으면 기본 워크스페이스 생성
    if await workspaces_collection.count_documents({}) == 0:
        default_workspace = {
            "name": "실험실",
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }
        workspace_id = await workspaces_collection.insert_one(default_workspace)
        
        # 기본 채널 생성
        default_channels = [
            {
                "name": "전체",
                "workspace_id": workspace_id.inserted_id,
                "description": "모든 구성원을 위한 채널",
                "created_at": datetime.now(),
                "updated_at": datetime.now()
            },
            {
                "name": "소셜",
                "workspace_id": workspace_id.inserted_id,
                "description": "일반적인 대화를 위한 채널",
                "created_at": datetime.now(),
                "updated_at": datetime.now()
            }
        ]
        await channels_collection.insert_many(default_channels)
        logger.info("기본 워크스페이스와 채널이 생성되었습니다.")

async def handle_client(websocket):
    """클라이언트 연결 처리"""
    clients.add(websocket)
    client_info[websocket] = {"user_id": None, "workspace": None}
    logger.info(f"클라이언트 연결됨. 현재 연결: {len(clients)}개")
    
    try:
        async for message in websocket:
            logger.info(f"수신: {message[:200]}...")  # 메시지가 너무 길 수 있으므로 일부만 로깅
            await handle_message(message, websocket)
    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f"클라이언트 연결 종료: {e}")
    except Exception as e:
        logger.error(f"처리 중 오류 발생: {e}")
    finally:
        clients.remove(websocket)
        if websocket in client_info:
            del client_info[websocket]
        logger.info(f"클라이언트 연결 해제. 현재 연결: {len(clients)}개")

async def handle_message(message, websocket):
    """클라이언트 메시지 처리"""
    try:
        data = json.loads(message)
        action = data.get("action")
        
        # 유저 정보 업데이트
        username = data.get("sender", "Unknown")
        if username != "Unknown" and username != "Server":
            # 사용자가 존재하는지 확인하고 없으면 생성
            user = await users_collection.find_one({"username": username})
            if not user:
                user_id = await users_collection.insert_one({
                    "username": username,
                    "created_at": datetime.now()
                })
                user_id = user_id.inserted_id
            else:
                user_id = user["_id"]
            
            # 클라이언트 정보 업데이트
            client_info[websocket]["user_id"] = user_id
            
        # 액션 처리
        if action == "send_message":
            await store_message(data)
            await broadcast(message, websocket)
        elif action == "register_user":
            await register_user(data, websocket)
        elif action == "get_workspace_list":
            await send_workspace_list(data, websocket)
        elif action == "get_channel_list":
            await send_channel_list(data, websocket)
        elif action == "get_channel_data":
            await send_channel_data(data, websocket)
        elif action == "create_workspace":
            await create_workspace(data, websocket)
        elif action == "create_channel":
            await create_channel(data, websocket)
        elif action == "delete_workspace":
            await delete_workspace(data, websocket)
        elif action == "delete_channel":
            await delete_channel(data, websocket)
        elif action == "update_workspace":
            await update_workspace(data, websocket)
        elif action == "update_channel":
            await update_channel(data, websocket)
        elif action == "search":
            await search_messages(data, websocket)
        else:
            logger.warning(f"알 수 없는 액션: {action}")
    except json.JSONDecodeError:
        logger.error(f"JSON 디코딩 오류: {message}")
    except Exception as e:
        logger.error(f"메시지 처리 중 오류 발생: {str(e)}")

async def register_user(data, websocket):
    """사용자 등록/업데이트"""
    username = data.get("username")
    if not username:
        return
        
    try:
        user = await users_collection.find_one({"username": username})
        if not user:
            result = await users_collection.insert_one({
                "username": username,
                "created_at": datetime.now()
            })
            user_id = result.inserted_id
        else:
            user_id = user["_id"]
            
        client_info[websocket]["user_id"] = user_id
        
        # 응답 전송
        response = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "register_user_response",
            "status": "success",
            "message": f"사용자 '{username}' 등록 성공"
        }
        await websocket.send(json.dumps(response))
    except Exception as e:
        logger.error(f"사용자 등록 중 오류: {str(e)}")
        response = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "register_user_response",
            "status": "error",
            "message": f"사용자 등록 중 오류 발생: {str(e)}"
        }
        await websocket.send(json.dumps(response))

async def store_message(data):
    """메시지를 MongoDB에 저장"""
    try:
        # 필요한 데이터 추출
        workspace_name = data.get("workspace", "default")
        channel_name = data.get("channel", "default").strip("#")
        sender = data.get("sender", "Unknown")
        
        # 워크스페이스 ID 찾기
        workspace = await workspaces_collection.find_one({"name": workspace_name})
        if not workspace:
            # 없으면 기본 워크스페이스 사용
            workspace = await workspaces_collection.find_one({})
            
        # 채널 ID 찾기
        channel = await channels_collection.find_one({
            "name": channel_name,
            "workspace_id": workspace["_id"]
        })
        
        if not channel:
            # 없으면 기본 채널 사용
            channel = await channels_collection.find_one({"workspace_id": workspace["_id"]})
        
        # 사용자 ID 찾기
        user = await users_collection.find_one({"username": sender})
        if not user:
            user_result = await users_collection.insert_one({
                "username": sender,
                "created_at": datetime.now()
            })
            user_id = user_result.inserted_id
        else:
            user_id = user["_id"]
            
        # 메시지 저장
        message_data = {
            "workspace_id": workspace["_id"],
            "channel_id": channel["_id"],
            "user_id": user_id,
            "sender": sender,
            "content": data.get("message", ""),
            "date": data.get("date", datetime.now().strftime('%Y-%m-%d')),
            "time": data.get("time", datetime.now().strftime("%H:%M:%S")),
            "created_at": datetime.now()
        }
        
        await messages_collection.insert_one(message_data)
        logger.info(f"메시지 저장됨: {sender} -> {channel_name}")
    except Exception as e:
        logger.error(f"메시지 저장 중 오류: {str(e)}")

async def create_workspace(data, websocket):
    """새 워크스페이스 생성"""
    workspace_name = data.get("workspace_name", "").strip()
    if not workspace_name:
        response = {
            "action": "create_workspace_response",
            "status": "error",
            "message": "워크스페이스 이름이 필요합니다."
        }
        await websocket.send(json.dumps(response))
        return
        
    try:
        # 이미 존재하는지 확인
        existing = await workspaces_collection.find_one({"name": workspace_name})
        if existing:
            response = {
                "action": "create_workspace_response",
                "status": "error",
                "message": f"'{workspace_name}' 워크스페이스가 이미 존재합니다."
            }
        else:
            # 새 워크스페이스 생성
            workspace = {
                "name": workspace_name,
                "created_at": datetime.now(),
                "updated_at": datetime.now()
            }
            result = await workspaces_collection.insert_one(workspace)
            workspace_id = result.inserted_id
            
            # 기본 채널 생성
            default_channels = [
                {
                    "name": "전체",
                    "workspace_id": workspace_id,
                    "description": "모든 구성원을 위한 채널",
                    "created_at": datetime.now(),
                    "updated_at": datetime.now()
                },
                {
                    "name": "소셜",
                    "workspace_id": workspace_id,
                    "description": "일반적인 대화를 위한 채널",
                    "created_at": datetime.now(),
                    "updated_at": datetime.now()
                }
            ]
            await channels_collection.insert_many(default_channels)
            
            response = {
                "action": "create_workspace_response",
                "status": "success",
                "message": f"'{workspace_name}' 워크스페이스가 생성되었습니다.",
                "workspace_id": str(workspace_id)
            }
            
        await websocket.send(json.dumps(response))
        # 모든 클라이언트에게 워크스페이스 목록 업데이트 알림
        await broadcast_workspace_update()
    except Exception as e:
        response = {
            "action": "create_workspace_response",
            "status": "error",
            "message": f"워크스페이스 생성 중 오류 발생: {str(e)}"
        }
        await websocket.send(json.dumps(response))
        logger.error(f"워크스페이스 생성 중 오류: {str(e)}")

async def create_channel(data, websocket):
    """새 채널 생성"""
    workspace_name = data.get("workspace", "").strip()
    channel_name = data.get("channel_name", "").strip()
    description = data.get("description", "")
    
    if not channel_name:
        response = {
            "action": "create_channel_response",
            "status": "error",
            "message": "채널 이름이 필요합니다."
        }
        await websocket.send(json.dumps(response))
        return
        
    try:
        # 워크스페이스 찾기
        workspace = await workspaces_collection.find_one({"name": workspace_name})
        if not workspace:
            response = {
                "action": "create_channel_response",
                "status": "error",
                "message": f"'{workspace_name}' 워크스페이스를 찾을 수 없습니다."
            }
            await websocket.send(json.dumps(response))
            return
            
        # 이미 존재하는 채널인지 확인
        existing = await channels_collection.find_one({
            "name": channel_name,
            "workspace_id": workspace["_id"]
        })
        
        if existing:
            response = {
                "action": "create_channel_response",
                "status": "error",
                "message": f"'{channel_name}' 채널이 해당 워크스페이스에 이미 존재합니다."
            }
        else:
            # 새 채널 생성
            channel = {
                "name": channel_name,
                "workspace_id": workspace["_id"],
                "description": description,
                "created_at": datetime.now(),
                "updated_at": datetime.now()
            }
            result = await channels_collection.insert_one(channel)
            
            response = {
                "action": "create_channel_response",
                "status": "success",
                "message": f"'{channel_name}' 채널이 생성되었습니다.",
                "channel_id": str(result.inserted_id)
            }
            
        await websocket.send(json.dumps(response))
        # 해당 워크스페이스의 모든 클라이언트에게 채널 목록 업데이트 알림
        await broadcast_channel_update(workspace_name)
    except Exception as e:
        response = {
            "action": "create_channel_response",
            "status": "error",
            "message": f"채널 생성 중 오류 발생: {str(e)}"
        }
        await websocket.send(json.dumps(response))
        logger.error(f"채널 생성 중 오류: {str(e)}")

async def delete_workspace(data, websocket):
    """워크스페이스 삭제"""
    workspace_name = data.get("workspace", "").strip()
    
    if not workspace_name:
        response = {
            "action": "delete_workspace_response",
            "status": "error",
            "message": "워크스페이스 이름이 필요합니다."
        }
        await websocket.send(json.dumps(response))
        return
        
    try:
        # 워크스페이스 찾기
        workspace = await workspaces_collection.find_one({"name": workspace_name})
        if not workspace:
            response = {
                "action": "delete_workspace_response",
                "status": "error",
                "message": f"'{workspace_name}' 워크스페이스를 찾을 수 없습니다."
            }
            await websocket.send(json.dumps(response))
            return
            
        # 워크스페이스 관련 채널 삭제
        await channels_collection.delete_many({"workspace_id": workspace["_id"]})
        
        # 워크스페이스 관련 메시지 삭제
        await messages_collection.delete_many({"workspace_id": workspace["_id"]})
        
        # 워크스페이스 삭제
        await workspaces_collection.delete_one({"_id": workspace["_id"]})
        
        response = {
            "action": "delete_workspace_response",
            "status": "success",
            "message": f"'{workspace_name}' 워크스페이스가 삭제되었습니다."
        }
        
        await websocket.send(json.dumps(response))
        # 모든 클라이언트에게 워크스페이스 목록 업데이트 알림
        await broadcast_workspace_update()
    except Exception as e:
        response = {
            "action": "delete_workspace_response",
            "status": "error",
            "message": f"워크스페이스 삭제 중 오류 발생: {str(e)}"
        }
        await websocket.send(json.dumps(response))
        logger.error(f"워크스페이스 삭제 중 오류: {str(e)}")

async def delete_channel(data, websocket):
    """채널 삭제"""
    workspace_name = data.get("workspace", "").strip()
    channel_name = data.get("channel", "").strip().strip("#")
    
    if not channel_name or not workspace_name:
        response = {
            "action": "delete_channel_response",
            "status": "error",
            "message": "워크스페이스와 채널 이름이 필요합니다."
        }
        await websocket.send(json.dumps(response))
        return
        
    try:
        # 워크스페이스 찾기
        workspace = await workspaces_collection.find_one({"name": workspace_name})
        if not workspace:
            response = {
                "action": "delete_channel_response",
                "status": "error",
                "message": f"'{workspace_name}' 워크스페이스를 찾을 수 없습니다."
            }
            await websocket.send(json.dumps(response))
            return
            
        # 채널 찾기
        channel = await channels_collection.find_one({
            "name": channel_name,
            "workspace_id": workspace["_id"]
        })
        
        if not channel:
            response = {
                "action": "delete_channel_response",
                "status": "error",
                "message": f"'{channel_name}' 채널을 찾을 수 없습니다."
            }
            await websocket.send(json.dumps(response))
            return
            
        # 채널의 메시지 삭제
        await messages_collection.delete_many({"channel_id": channel["_id"]})
        
        # 채널 삭제
        await channels_collection.delete_one({"_id": channel["_id"]})
        
        response = {
            "action": "delete_channel_response",
            "status": "success",
            "message": f"'{channel_name}' 채널이 삭제되었습니다."
        }
        
        await websocket.send(json.dumps(response))
        # 해당 워크스페이스의 모든 클라이언트에게 채널 목록 업데이트 알림
        await broadcast_channel_update(workspace_name)
    except Exception as e:
        response = {
            "action": "delete_channel_response",
            "status": "error",
            "message": f"채널 삭제 중 오류 발생: {str(e)}"
        }
        await websocket.send(json.dumps(response))
        logger.error(f"채널 삭제 중 오류: {str(e)}")

async def update_workspace(data, websocket):
    """워크스페이스 업데이트"""
    old_name = data.get("old_name", "").strip()
    new_name = data.get("new_name", "").strip()
    
    if not old_name or not new_name:
        response = {
            "action": "update_workspace_response",
            "status": "error",
            "message": "기존 이름과 새 이름이 필요합니다."
        }
        await websocket.send(json.dumps(response))
        return
        
    try:
        # 워크스페이스 찾기
        workspace = await workspaces_collection.find_one({"name": old_name})
        if not workspace:
            response = {
                "action": "update_workspace_response",
                "status": "error",
                "message": f"'{old_name}' 워크스페이스를 찾을 수 없습니다."
            }
            await websocket.send(json.dumps(response))
            return
            
        # 이름 중복 확인
        if old_name != new_name:
            existing = await workspaces_collection.find_one({"name": new_name})
            if existing:
                response = {
                    "action": "update_workspace_response",
                    "status": "error",
                    "message": f"'{new_name}' 워크스페이스가 이미 존재합니다."
                }
                await websocket.send(json.dumps(response))
                return
        
        # 워크스페이스 업데이트
        await workspaces_collection.update_one(
            {"_id": workspace["_id"]},
            {"$set": {"name": new_name, "updated_at": datetime.now()}}
        )
        
        response = {
            "action": "update_workspace_response",
            "status": "success",
            "message": f"워크스페이스 이름이 '{old_name}'에서 '{new_name}'으로 변경되었습니다."
        }
        
        await websocket.send(json.dumps(response))
        # 모든 클라이언트에게 워크스페이스 목록 업데이트 알림
        await broadcast_workspace_update()
    except Exception as e:
        response = {
            "action": "update_workspace_response",
            "status": "error",
            "message": f"워크스페이스 업데이트 중 오류 발생: {str(e)}"
        }
        await websocket.send(json.dumps(response))
        logger.error(f"워크스페이스 업데이트 중 오류: {str(e)}")

async def update_channel(data, websocket):
    """채널 업데이트"""
    workspace_name = data.get("workspace", "").strip()
    old_name = data.get("old_name", "").strip()
    new_name = data.get("new_name", "").strip()
    description = data.get("description", None)
    
    if not workspace_name or not old_name or not new_name:
        response = {
            "action": "update_channel_response",
            "status": "error",
            "message": "워크스페이스 이름, 기존 채널 이름, 새 채널 이름이 필요합니다."
        }
        await websocket.send(json.dumps(response))
        return
        
    try:
        # 워크스페이스 찾기
        workspace = await workspaces_collection.find_one({"name": workspace_name})
        if not workspace:
            response = {
                "action": "update_channel_response",
                "status": "error",
                "message": f"'{workspace_name}' 워크스페이스를 찾을 수 없습니다."
            }
            await websocket.send(json.dumps(response))
            return
            
        # 채널 찾기
        channel = await channels_collection.find_one({
            "name": old_name,
            "workspace_id": workspace["_id"]
        })
        
        if not channel:
            response = {
                "action": "update_channel_response",
                "status": "error",
                "message": f"'{old_name}' 채널을 찾을 수 없습니다."
            }
            await websocket.send(json.dumps(response))
            return
            
        # 이름 중복 확인
        if old_name != new_name:
            existing = await channels_collection.find_one({
                "name": new_name,
                "workspace_id": workspace["_id"]
            })
            
            if existing:
                response = {
                    "action": "update_channel_response",
                    "status": "error",
                    "message": f"'{new_name}' 채널이 해당 워크스페이스에 이미 존재합니다."
                }
                await websocket.send(json.dumps(response))
                return
                
        # 업데이트할 필드
        update_fields = {"name": new_name, "updated_at": datetime.now()}
        if description is not None:
            update_fields["description"] = description
        
        # 채널 업데이트
        await channels_collection.update_one(
            {"_id": channel["_id"]},
            {"$set": update_fields}
        )
        
        response = {
            "action": "update_channel_response",
            "status": "success",
            "message": f"채널 이름이 '{old_name}'에서 '{new_name}'으로 변경되었습니다."
        }
        
        await websocket.send(json.dumps(response))
        # 해당 워크스페이스의 모든 클라이언트에게 채널 목록 업데이트 알림
        await broadcast_channel_update(workspace_name)
    except Exception as e:
        response = {
            "action": "update_channel_response",
            "status": "error",
            "message": f"채널 업데이트 중 오류 발생: {str(e)}"
        }
        await websocket.send(json.dumps(response))
        logger.error(f"채널 업데이트 중 오류: {str(e)}")

async def send_workspace_list(data, websocket):
    """워크스페이스 목록 전송"""
    try:
        workspaces = {}
        # 모든 워크스페이스 찾기
        cursor = workspaces_collection.find({}).sort("name", 1)
        async for workspace in cursor:
            workspace_name = workspace["name"]
            workspaces[workspace_name] = []
            
            # 해당 워크스페이스의 채널 찾기
            channel_cursor = channels_collection.find({"workspace_id": workspace["_id"]}).sort("name", 1)
            async for channel in channel_cursor:
                workspaces[workspace_name].append(channel["name"])
                
        response = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "workspace_list",
            "message": workspaces
        }
        
        await websocket.send(json.dumps(response))
    except Exception as e:
        logger.error(f"워크스페이스 목록 전송 중 오류: {str(e)}")
        response = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "workspace_list",
            "status": "error",
            "message": {}
        }
        await websocket.send(json.dumps(response))

async def send_channel_list(data, websocket):
    """특정 워크스페이스의 채널 목록 전송"""
    workspace_name = data.get("workspace", "default")
    
    try:
        # 워크스페이스 찾기
        workspace = await workspaces_collection.find_one({"name": workspace_name})
        
        channels = []
        if workspace:
            # 해당 워크스페이스의 채널 찾기
            channel_cursor = channels_collection.find({"workspace_id": workspace["_id"]}).sort("name", 1)
            async for channel in channel_cursor:
                channels.append(channel["name"])
                
        response = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "channel_list",
            "workspace": workspace_name,
            "message": channels
        }
        
        await websocket.send(json.dumps(response))
        
        # 클라이언트 정보 업데이트
        client_info[websocket]["workspace"] = workspace_name
    except Exception as e:
        logger.error(f"채널 목록 전송 중 오류: {str(e)}")
        response = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "channel_list",
            "status": "error",
            "message": []
        }
        await websocket.send(json.dumps(response))

async def send_channel_data(data, websocket):
    """특정 채널의 메시지 데이터 전송"""
    workspace_name = data.get("workspace", "default")
    channel_name = data.get("channel", "default").strip("#")
    
    try:
        # 워크스페이스 찾기
        workspace = await workspaces_collection.find_one({"name": workspace_name})
        if not workspace:
            response = {
                "date": datetime.now().strftime('%Y-%m-%d'),
                "time": datetime.now().strftime("%H:%M:%S"),
                "sender": "Server",
                "action": "channel_data",
                "status": "error",
                "message": f"워크스페이스 '{workspace_name}'를 찾을 수 없습니다.",
                "data": []
            }
            await websocket.send(json.dumps(response))
            return
            
        # 채널 찾기
        channel = await channels_collection.find_one({
            "name": channel_name,
            "workspace_id": workspace["_id"]
        })
        
        if not channel:
            response = {
                "date": datetime.now().strftime('%Y-%m-%d'),
                "time": datetime.now().strftime("%H:%M:%S"),
                "sender": "Server",
                "action": "channel_data",
                "status": "error",
                "message": f"채널 '{channel_name}'을 찾을 수 없습니다.",
                "data": []
            }
            await websocket.send(json.dumps(response))
            return
            
        # 채널 메시지 가져오기
        messages = []
        message_cursor = messages_collection.find({
            "workspace_id": workspace["_id"],
            "channel_id": channel["_id"]
        }).sort("created_at", 1)
        
        async for msg in message_cursor:
            messages.append({
                "date": msg.get("date"),
                "time": msg.get("time"),
                "sender": msg.get("sender"),
                "message": msg.get("content")
            })
            
        response = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "channel_data",
            "workspace": workspace_name,
            "channel": channel_name,
            "message": messages
        }
        
        await websocket.send(json.dumps(response))
    except Exception as e:
        logger.error(f"채널 데이터 전송 중 오류: {str(e)}")
        response = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "channel_data",
            "status": "error",
            "message": [],
            "error": str(e)
        }
        await websocket.send(json.dumps(response))

async def search_messages(data, websocket):
    """메시지 검색"""
    query = data.get("query", "").strip()
    workspace_name = data.get("workspace", None)
    channel_name = data.get("channel", None)
    sender = data.get("sender", None)
    date_from = data.get("date_from", None)
    date_to = data.get("date_to", None)
    
    if not query:
        response = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "search_response",
            "status": "error",
            "message": "검색어가 필요합니다.",
            "results": []
        }
        await websocket.send(json.dumps(response))
        return
        
    try:
        search_filter = {"content": {"$regex": query, "$options": "i"}}
        
        # 워크스페이스 필터 추가
        if workspace_name:
            workspace = await workspaces_collection.find_one({"name": workspace_name})
            if workspace:
                search_filter["workspace_id"] = workspace["_id"]
                
        # 채널 필터 추가
        if channel_name and workspace_name:
            workspace = await workspaces_collection.find_one({"name": workspace_name})
            if workspace:
                channel = await channels_collection.find_one({
                    "name": channel_name,
                    "workspace_id": workspace["_id"]
                })
                if channel:
                    search_filter["channel_id"] = channel["_id"]
                    
        # 보낸 사람 필터 추가
        if sender:
            search_filter["sender"] = {"$regex": sender, "$options": "i"}
            
        # 날짜 필터 추가
        date_filter = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        if date_filter:
            search_filter["date"] = date_filter
            
        # 검색 실행
        results = []
        message_cursor = messages_collection.find(search_filter).sort("created_at", -1).limit(100)
        
        async for msg in message_cursor:
            # 워크스페이스 정보 가져오기
            workspace = await workspaces_collection.find_one({"_id": msg["workspace_id"]})
            workspace_name = workspace["name"] if workspace else "Unknown"
            
            # 채널 정보 가져오기
            channel = await channels_collection.find_one({"_id": msg["channel_id"]})
            channel_name = channel["name"] if channel else "Unknown"
            
            results.append({
                "date": msg.get("date"),
                "time": msg.get("time"),
                "sender": msg.get("sender"),
                "message": msg.get("content"),
                "workspace": workspace_name,
                "channel": channel_name
            })
            
        response = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "search_response",
            "status": "success",
            "query": query,
            "count": len(results),
            "results": results
        }
        
        await websocket.send(json.dumps(response))
    except Exception as e:
        logger.error(f"메시지 검색 중 오류: {str(e)}")
        response = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "search_response",
            "status": "error",
            "message": f"검색 중 오류 발생: {str(e)}",
            "results": []
        }
        await websocket.send(json.dumps(response))

async def broadcast(message, sender):
    """모든 클라이언트에게 메시지 브로드캐스팅"""
    for client in clients:
        if client != sender:
            try:
                await client.send(message)
            except Exception as e:
                logger.error(f"브로드캐스팅 중 오류: {str(e)}")

async def broadcast_workspace_update():
    """모든 클라이언트에게 워크스페이스 목록 업데이트 알림"""
    workspaces = {}
    
    try:
        # 모든 워크스페이스 찾기
        cursor = workspaces_collection.find().sort("name", 1)
        async for workspace in cursor:
            workspace_name = workspace["name"]
            workspaces[workspace_name] = []
            
            # 해당 워크스페이스의 채널 찾기
            channel_cursor = channels_collection.find({"workspace_id": workspace["_id"]}).sort("name", 1)
            async for channel in channel_cursor:
                workspaces[workspace_name].append(channel["name"])
                
        notification = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "workspace_update",
            "message": workspaces
        }
        
        for client in clients:
            try:
                await client.send(json.dumps(notification))
            except Exception as e:
                logger.error(f"워크스페이스 업데이트 알림 중 오류: {str(e)}")
    except Exception as e:
        logger.error(f"워크스페이스 업데이트 알림 생성 중 오류: {str(e)}")

async def broadcast_channel_update(workspace_name):
    """특정 워크스페이스의 모든 클라이언트에게 채널 목록 업데이트 알림"""
    channels = []
    
    try:
        # 워크스페이스 찾기
        workspace = await workspaces_collection.find_one({"name": workspace_name})
        
        if workspace:
            # 해당 워크스페이스의 채널 찾기
            channel_cursor = channels_collection.find({"workspace_id": workspace["_id"]}).sort("name", 1)
            async for channel in channel_cursor:
                channels.append(channel["name"])
                
        notification = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime("%H:%M:%S"),
            "sender": "Server",
            "action": "channel_update",
            "workspace": workspace_name,
            "message": channels
        }
        
        # 해당 워크스페이스를 구독 중인 클라이언트에게만 전송
        for client, info in client_info.items():
            if info.get("workspace") == workspace_name:
                try:
                    await client.send(json.dumps(notification))
                except Exception as e:
                    logger.error(f"채널 업데이트 알림 중 오류: {str(e)}")
    except Exception as e:
        logger.error(f"채널 업데이트 알림 생성 중 오류: {str(e)}")

async def main():
    """메인 서버 실행 함수"""
    # 데이터베이스 초기화
    await initialize_db()
    
    # 웹소켓 서버 시작
    async with websockets.serve(handle_client, HOST, PORT):
        logger.info(f"서버가 {HOST}:{PORT}에서 시작되었습니다.")
        await asyncio.Future()  # 무한 실행

if __name__ == "__main__":
    asyncio.run(main())