#!/usr/bin/env python3
"""
Room Host Script for Ubiq Server
Creates a room, waits for peers to join, and broadcasts messages to all peers.
"""

import asyncio
import socket
import struct
import json
import uuid
import time
import math
import os
import base64
import numpy as np
import threading
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field

# Import for STL file handling
try:
    from stl import mesh
    STL_AVAILABLE = True
except ImportError:
    STL_AVAILABLE = False
    print("⚠️ Warning: numpy-stl not installed. STL loading will be disabled.")
    print("   Install with: pip install numpy-stl")


@dataclass
class NetworkId:
    """Represents a NetworkId with two 32-bit integers"""
    a: int
    b: int
    
    @classmethod
    def from_number(cls, num: int) -> 'NetworkId':
        """Create NetworkId from a single number"""
        return cls(0, num)
    
    @classmethod
    def from_string(cls, s: str) -> 'NetworkId':
        """Create NetworkId from hex string"""
        s = s.replace('-', '')
        a = int(s[:8], 16) if len(s) >= 8 else 0
        b = int(s[8:16], 16) if len(s) >= 16 else 0
        return cls(a, b)
    
    @classmethod
    def create(cls, namespace: 'NetworkId', service: str) -> 'NetworkId':
        """Create a namespaced NetworkId like the TypeScript version"""
        # Start with the namespace
        data = [namespace.a, namespace.b]
        
        # Process the service string byte by byte
        service_bytes = service.encode('utf-8')
        for i, byte_val in enumerate(service_bytes):
            if i % 2 != 0:
                # Use 32-bit signed integer multiplication
                data[0] = cls._imul_32(data[0], byte_val)
            else:
                data[1] = cls._imul_32(data[1], byte_val)
        
        return cls(data[0], data[1])
    
    @staticmethod
    def _imul_32(a: int, b: int) -> int:
        """32-bit signed integer multiplication (like Math.imul)"""
        # Convert to 32-bit signed integers
        a = ((a & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000
        b = ((b & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000
        
        # Multiply and convert back to 32-bit signed
        result = (a * b) & 0xFFFFFFFF
        return ((result ^ 0x80000000) - 0x80000000)
    
    def to_bytes(self) -> bytes:
        """Convert to bytes for network transmission"""
        return struct.pack('<II', self.a & 0xFFFFFFFF, self.b & 0xFFFFFFFF)
    
    def __str__(self) -> str:
        return f"{self.a:x}-{self.b:x}"


@dataclass
class PeerInfo:
    """Information about a peer"""
    uuid: str
    sceneid: int
    clientid: NetworkId
    keys: List[str] = field(default_factory=list)
    values: List[str] = field(default_factory=list)


class Message:
    """Represents a network message"""
    
    def __init__(self, network_id: NetworkId, data: Any):
        self.network_id = network_id
        self.data = data
    
    def to_bytes(self) -> bytes:
        """Convert message to bytes for network transmission"""
        # Convert data to JSON string if it's not already a string
        if isinstance(self.data, str):
            json_data = self.data.encode('utf-8')
        elif isinstance(self.data, dict):
            json_data = json.dumps(self.data).encode('utf-8')
        else:
            json_data = str(self.data).encode('utf-8')
        
        # Message format: [length(4)] [networkId(8)] [json_data]
        length = len(json_data) + 8  # 8 bytes for networkId
        
        buffer = bytearray()
        buffer.extend(struct.pack('<I', length))  # Length (little-endian)
        buffer.extend(self.network_id.to_bytes())  # NetworkId (8 bytes)
        buffer.extend(json_data)  # JSON data
        
        return bytes(buffer)
    
    @classmethod
    def from_bytes(cls, data: bytes) -> 'Message':
        """Parse message from bytes"""
        if len(data) < 12:  # 4 + 8 minimum
            raise ValueError("Message too short")
        
        length = struct.unpack('<I', data[0:4])[0]
        network_id_bytes = data[4:12]
        message_bytes = data[12:12+(length-8)]
        
        # Parse NetworkId
        a, b = struct.unpack('<II', network_id_bytes)
        network_id = NetworkId(a, b)
        
        # Try to parse as JSON first, fall back to raw bytes if it fails
        try:
            json_data = json.loads(message_bytes.decode('utf-8'))
            return cls(network_id, json_data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            # This is likely a binary message (e.g., peer-to-peer connection data)
            return cls(network_id, message_bytes)


class RoomPeer:
    """Represents a peer in a room"""
    
    def __init__(self, peer_info: PeerInfo):
        self.uuid = peer_info.uuid
        self.sceneid = peer_info.sceneid
        self.clientid = peer_info.clientid
        self.properties: Dict[str, str] = {}
        self.join_time = time.time()
        
        # Set properties from keys/values
        for key, value in zip(peer_info.keys, peer_info.values):
            self.properties[key] = value
    
    def get_property(self, key: str) -> Optional[str]:
        """Get a property value"""
        return self.properties.get(key)


class Room:
    """Represents a room"""
    
    def __init__(self):
        self.uuid = ""
        self.joincode = ""
        self.publish = False
        self.name = "Uninitialised Room Container"
        self.properties: Dict[str, str] = {}


@dataclass
class STLData:
    """Represents STL mesh data for broadcasting"""
    filename: str
    vertices: List[float]        # Flattened list of vertices (x1,y1,z1,x2,y2,z2,...)
    normals: List[float]         # Flattened list of normals (nx1,ny1,nz1,nx2,ny2,nz2,...)
    vertex_count: int
    triangle_count: int
    bounds: Dict[str, float]     # min/max x, y, z values
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert STL data to dictionary for JSON serialization"""
        return {
            "filename": self.filename,
            "vertices": self.vertices,
            "normals": self.normals,
            "vertex_count": self.vertex_count,
            "triangle_count": self.triangle_count,
            "bounds": self.bounds
        }


class RoomHost:
    """Python Room Host that creates a room and manages peers"""
    
    def __init__(self, host: str = "localhost", port: int = 8009):
        self.host = host
        self.port = port
        self.socket: Optional[socket.socket] = None
        self.running = False
        
        # Generate unique IDs - ensure they're different each time
        timestamp = int(time.time() * 1000)
        random_component = int(time.time() * 1000000) % 0xFFFFFFFF  # More randomness
        self.scene_id = NetworkId.from_number(random_component)
        self.network_id = NetworkId.create(self.scene_id, "RoomClient")
        self.server_network_id = NetworkId.from_number(1)
        
        # Initialize peer with unique UUID each time
        self.peer = RoomPeer(PeerInfo(
            uuid=str(uuid.uuid4()),
            sceneid=self.scene_id.b,
            clientid=self.network_id
        ))
        
        # Initialize room and peers
        self.room = Room()
        self.peers: Dict[str, RoomPeer] = {self.peer.uuid: self.peer}
        
        # Event callbacks
        self.callbacks: Dict[str, List[Callable]] = {}
        
        # Message broadcasting
        self.broadcast_counter = 0
        self.last_ping_time = 0
        
        # Room discovery
        self.discovered_rooms = []
        self.room_discovery_complete = False
        self.target_room_name = ""
        self.join_attempt_count = 0
        self.join_rejected = False
        
        # STL data storage
        self.stl_data: Optional[STLData] = None
        self.stl_broadcast_enabled = False
        self.stl_thread: Optional[threading.Thread] = None
        
        print(f"🎯 Room Host Initialized:")
        print(f"   Scene ID: {self.scene_id}")
        print(f"   Network ID: {self.network_id}")
        print(f"   Host Peer UUID: {self.peer.uuid}")
    
    def add_listener(self, event: str, callback: Callable):
        """Add an event listener"""
        if event not in self.callbacks:
            self.callbacks[event] = []
        self.callbacks[event].append(callback)
    
    def emit(self, event: str, *args):
        """Emit an event to all listeners"""
        if event in self.callbacks:
            for callback in self.callbacks[event]:
                try:
                    callback(*args)
                except Exception as e:
                    print(f"Error in callback for {event}: {e}")
    
    async def connect(self):
        """Connect to the server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(10.0)
            self.socket.connect((self.host, self.port))
            self.socket.settimeout(None)
            print(f"✅ Connected to server at {self.host}:{self.port}")
            return True
        except Exception as e:
            print(f"❌ Failed to connect: {e}")
            return False
    
    def is_connected(self) -> bool:
        """Check if socket is still connected"""
        if not self.socket:
            return False
        
        try:
            error = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            return error == 0
        except socket.error:
            return False
    
    def disconnect(self):
        """Disconnect from the server"""
        self.running = False
        if self.socket:
            self.socket.close()
            self.socket = None
            print("🔌 Disconnected from server")
        
        # Stop STL broadcast thread if running
        if self.stl_thread and self.stl_thread.is_alive():
            print("🔄 Stopping STL broadcast thread...")
            self.stl_thread.join(timeout=2.0)  # Wait up to 2 seconds
            if self.stl_thread.is_alive():
                print("⚠️ STL broadcast thread did not stop gracefully")
            else:
                print("✅ STL broadcast thread stopped")
    
    def send_server_message(self, message_type: str, args: Dict[str, Any]):
        """Send a message to the server"""
        if not self.socket:
            print("❌ Not connected to server")
            return False
        
        # Create message exactly like TypeScript: 
        # The message content should be a JSON object with type and args fields
        # where args is a JSON string
        message_content = {
            "type": message_type,
            "args": json.dumps(args)  # args should be JSON string
        }
        
        # Convert the message content to JSON string (this will be the message body)
        json_string = json.dumps(message_content)
        
        # Create the network message with the JSON string as data
        message = Message(self.server_network_id, json_string)
        data = message.to_bytes()
        
        # Only show debug output for non-ping messages
        if message_type != "Ping":
            print(f"📤 Sending {message_type}: {args}")
            print(f"Message content: {json_string}")
        
        try:
            sent = self.socket.send(data)
            if sent != len(data):
                print(f"⚠️ Warning: Only sent {sent} of {len(data)} bytes")
                return False
            return True
        except Exception as e:
            print(f"❌ Failed to send message: {e}")
            self.disconnect()  # Disconnect on send error
            return False
    
    def broadcast_to_peers(self, message_type: str, data: Dict[str, Any]):
        """Broadcast a message to all peers in the room using peer-to-peer messaging"""
        if not self.socket or len(self.peers) <= 1:
            return
        
        # Send a message to each peer individually
        messages_sent = 0
        for peer_uuid, peer in self.peers.items():
            if peer_uuid != self.peer.uuid:  # Don't send to self
                # Create the message content in the format Unity expects
                # Unity expects the args to be an object, not a JSON string
                message_content = {
                    "type": message_type,
                    "args": data  # Send as object, not JSON string
                }
                
                json_string = json.dumps(message_content)
                # Send directly to the peer's network ID
                message = Message(peer.clientid, json_string)
                message_data = message.to_bytes()
                
                try:
                    self.socket.send(message_data)
                    messages_sent += 1
                except Exception as e:
                    print(f"❌ Failed to send to peer {peer_uuid[:8]}: {e}")
        
        if messages_sent > 0:
            print(f"📡 Sent {message_type} message to {messages_sent} peer(s)")
        else:
            print(f"⚠️ No messages sent for {message_type}")
    
    def receive_message(self) -> Optional[Message]:
        """Receive a message from the server"""
        if not self.socket:
            return None
        
        try:
            import select
            ready, _, _ = select.select([self.socket], [], [], 0.1)
            if not ready:
                return None
            
            # Read length first
            length_data = b''
            while len(length_data) < 4:
                chunk = self.socket.recv(4 - len(length_data))
                if not chunk:
                    return None
                length_data += chunk
            
            length = struct.unpack('<I', length_data)[0]
            
            if length <= 0 or length > 1024 * 1024:
                print(f"❌ Invalid message length: {length}")
                return None
            
            # Read the rest of the message
            remaining = length
            data = bytearray(length_data)
            
            while remaining > 0:
                chunk = self.socket.recv(min(remaining, 4096))
                if not chunk:
                    return None
                data.extend(chunk)
                remaining -= len(chunk)
            
            return Message.from_bytes(bytes(data))
        
        except Exception as e:
            print(f"❌ Failed to receive message: {e}")
            # Don't immediately signal shutdown - let the message loop handle it
            return None
    
    def process_message(self, message: Message):
        """Process a received message"""
        try:
            data = message.data
            
            # Silently skip binary messages (real-time transform data from Unity)
            if isinstance(data, bytes):
                return  # Skip immediately without any processing or logging
            
            # Handle JSON messages
            if not isinstance(data, dict):
                print(f"⚠️ Unexpected message format: {type(data)}")
                return
                
            message_type = data.get("type")
            args_str = data.get("args", "{}")
            
            # Parse args - handle both string and dict formats
            if isinstance(args_str, str):
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = args_str if isinstance(args_str, dict) else {}
            
            # Don't log ping messages to reduce noise
            if message_type != "Ping":
                print(f"📥 Received {message_type}")
            
            if message_type == "SetRoom":
                self.set_room_info(args["room"])
                self.emit("OnJoinedRoom", self.room)
                print(f"✅ Room initialized successfully:")
                print(f"   Name: {self.room.name}")
                print(f"   UUID: {self.room.uuid}")
                print(f"   Join Code: {self.room.joincode}")
                print(f"   Published: {self.room.publish}")
            
            elif message_type == "PeerAdded":
                peer_data = args["peer"]
                sceneid_data = peer_data["sceneid"]
                
                if isinstance(sceneid_data, dict):
                    sceneid = sceneid_data.get("b", 0)
                else:
                    sceneid = sceneid_data
                
                peer_info = PeerInfo(
                    uuid=peer_data["uuid"],
                    sceneid=sceneid,
                    clientid=NetworkId(peer_data["clientid"]["a"], peer_data["clientid"]["b"]),
                    keys=peer_data.get("keys", []),
                    values=peer_data.get("values", [])
                )
                
                if peer_info.uuid not in self.peers:
                    peer = RoomPeer(peer_info)
                    self.peers[peer_info.uuid] = peer
                    self.emit("OnPeerAdded", peer)
                    print(f"✅ New peer added: {peer_info.uuid[:8]}...")
                else:
                    print(f"🔄 Peer already exists: {peer_info.uuid[:8]}...")
            
            elif message_type == "PeerRemoved":
                peer_uuid = args["uuid"]
                if peer_uuid in self.peers:
                    peer = self.peers[peer_uuid]
                    del self.peers[peer_uuid]
                    self.emit("OnPeerRemoved", peer)
                    print(f"❌ Peer removed: {peer_uuid[:8]}...")
                else:
                    print(f"⚠️ Unknown peer removed: {peer_uuid[:8]}...")
            
            elif message_type == "Ping":
                self.emit("OnPing", args.get("sessionId", ""))
                # Don't print ping responses to reduce noise
            
            elif message_type == "Rooms":
                self.discovered_rooms = args.get("rooms", [])
                self.room_discovery_complete = True
                self.emit("OnRoomsDiscovered", self.discovered_rooms)
            
            elif message_type == "Rejected":
                reason = args.get("reason", "Unknown reason")
                print(f"❌ Join rejected: {reason}")
                self.join_rejected = True
                self.emit("OnJoinRejected", reason)
            
            elif message_type == "RequestPeerConnection":
                # Handle Unity peer connection requests
                print(f"🔗 Peer connection request from Unity client")
                # For a room host, we typically don't need to establish peer connections
                # Unity clients will connect to each other directly
                pass
            
            else:
                if message_type:
                    print(f"⚠️ Unhandled message type: {message_type}")
                else:
                    print(f"⚠️ Message without type: {data}")
            
        except Exception as e:
            print(f"❌ Error processing message: {e}")
            print(f"   Message data type: {type(message.data)}")
            if hasattr(message, 'data') and isinstance(message.data, (dict, str)):
                print(f"   Message data: {message.data}")
    
    def set_room_info(self, room_info: Dict[str, Any]):
        """Set room information"""
        self.room.uuid = room_info.get("uuid", "")
        self.room.joincode = room_info.get("joincode", "")
        self.room.publish = room_info.get("publish", False)
        self.room.name = room_info.get("name", "")
        
        keys = room_info.get("keys", [])
        values = room_info.get("values", [])
        self.room.properties = dict(zip(keys, values))
    
    def get_peer_info(self) -> Dict[str, Any]:
        """Get peer information for sending to server"""
        return {
            "uuid": self.peer.uuid,
            "sceneid": {"a": self.scene_id.a, "b": self.scene_id.b},
            "clientid": {"a": self.network_id.a, "b": self.network_id.b},
            "keys": list(self.peer.properties.keys()),
            "values": list(self.peer.properties.values())
        }
    
    def ping_server(self):
        """Send a ping to the server to keep connection alive"""
        args = {
            "clientid": {"a": self.network_id.a, "b": self.network_id.b}
        }
        return self.send_server_message("Ping", args)
    
    def discover_rooms(self):
        """Discover existing rooms on the server"""
        if not self.socket:
            print("❌ Not connected to server")
            return False
        
        self.room_discovery_complete = False
        self.discovered_rooms = []
        
        print("🔍 Discovering existing rooms...")
        
        # Use the same approach as simple_room_monitor.py
        return self.send_server_message("DiscoverRooms", {
            "clientid": {"a": self.network_id.a, "b": self.network_id.b},
            "joincode": ""
        })
    
    def find_room_by_name(self, room_name: str) -> Optional[Dict[str, Any]]:
        """Find a room by name in discovered rooms"""
        for room in self.discovered_rooms:
            if room.get("name") == room_name:
                return room
        return None
    
    def join_existing_room(self, room_info: Dict[str, Any]) -> bool:
        """Join an existing room using its join code"""
        args = {
            "joincode": room_info.get("joincode", ""),
            "name": room_info.get("name", ""),
            "publish": room_info.get("publish", False),
            "peer": self.get_peer_info()
        }
        
        print(f"🚪 Joining existing room: {room_info.get('name')} (code: {room_info.get('joincode')})")
        return self.send_server_message("Join", args)
    
    def create_room(self, room_name: str = "Python Host Room", publish: bool = True):
        """Create a new room"""
        args = {
            "joincode": "",
            "name": room_name,
            "publish": publish,
            "peer": self.get_peer_info()
        }
        
        print(f"🏗️ Creating new room: {room_name}")
        return self.send_server_message("Join", args)
    
    async def join_or_create_room(self, room_name: str = "Python Host Room", publish: bool = True) -> bool:
        """Discover rooms first, then join existing or create new"""
        self.target_room_name = room_name
        self.join_attempt_count = 0
        
        # First, discover existing rooms
        if not self.discover_rooms():
            return False
        
        # Wait for discovery to complete
        max_wait = 5.0  # Wait up to 5 seconds for room discovery
        wait_time = 0.0
        while not self.room_discovery_complete and wait_time < max_wait:
            await asyncio.sleep(0.1)
            wait_time += 0.1
        
        if not self.room_discovery_complete:
            print("⚠️ Room discovery timed out, proceeding to create new room")
            return self.create_room(room_name, publish)
        
        # Check if room with target name already exists
        existing_room = self.find_room_by_name(room_name)
        
        if existing_room:
            print(f"✅ Found existing room with name '{room_name}'")
            print(f"   UUID: {existing_room.get('uuid')}")
            print(f"   Join Code: {existing_room.get('joincode')}")
            print(f"   Published: {existing_room.get('publish')}")
            print(f"🔄 Attempting to rejoin existing room...")
            
            # Try to join the existing room
            self.join_rejected = False
            self.join_attempt_count += 1
            success = self.join_existing_room(existing_room)
            
            if success:
                # Wait a bit to see if join was rejected
                await asyncio.sleep(2.0)
                
                if self.join_rejected:
                    print("⚠️ Join was rejected, creating new room instead")
                    # Create a new room with timestamp to make it unique
                    new_room_name = f"{room_name} ({int(time.time())})"
                    return self.create_room(new_room_name, publish)
                
                # Check if we actually joined (got a room)
                if not self.room.uuid:
                    print("⚠️ Join didn't complete, creating new room instead")
                    new_room_name = f"{room_name} (New)"
                    return self.create_room(new_room_name, publish)
                
                return True
            else:
                print("⚠️ Failed to send join message, creating new room instead")
                new_room_name = f"{room_name} (Fallback)"
                return self.create_room(new_room_name, publish)
        else:
            print(f"❌ No existing room found with name '{room_name}'")
            if self.discovered_rooms:
                print(f"📋 Found {len(self.discovered_rooms)} other room(s):")
                for i, room in enumerate(self.discovered_rooms[:5]):  # Show max 5 rooms
                    print(f"   {i+1}. {room.get('name')} (code: {room.get('joincode')})")
                if len(self.discovered_rooms) > 5:
                    print(f"   ... and {len(self.discovered_rooms) - 5} more")
            return self.create_room(room_name, publish)
    
    async def message_loop(self):
        """Main message processing loop"""
        self.running = True
        while self.socket and self.running:
            try:
                # Send ping every 5 seconds to keep connection alive
                current_time = time.time()
                if current_time - self.last_ping_time >= 5.0:
                    self.ping_server()
                    self.last_ping_time = current_time
                
                message = self.receive_message()
                if message:
                    self.process_message(message)
                else:
                    await asyncio.sleep(0.1)
            except Exception as e:
                print(f"❌ Error in message loop: {e}")
                # Don't immediately break - try to continue unless it's a critical error
                if not self.is_connected():
                    print("❌ Lost connection to server")
                    break
                await asyncio.sleep(0.5)  # Brief pause before retrying
    
    async def broadcast_loop(self):
        """Periodic broadcast loop"""
        while self.running and self.socket:
            await asyncio.sleep(10)  # Broadcast every 10 seconds
            
            if len(self.peers) > 1:  # Only broadcast if there are other peers
                self.broadcast_counter += 1
                
                # Send a test message first to verify the broadcast mechanism works
                broadcast_data = {
                    "message": "Hello from Python Host",
                    "timestamp": time.time(),
                    "peer_count": len(self.peers),
                    "room_name": self.room.name,
                    "room_code": self.room.joincode
                }
                
                print(f"🔊 Broadcasting test message #{self.broadcast_counter} to {len(self.peers)-1} peers")
                self.broadcast_to_peers("HostBroadcast", broadcast_data)
    
    async def stl_broadcast_loop(self):
        """Dedicated STL broadcasting loop that runs independently"""
        print("📐 STL broadcast thread started")
        
        while self.running and self.socket:
            await asyncio.sleep(15)  # Broadcast STL every 15 seconds
            
            # Only broadcast if STL is enabled, data is loaded, and there are peers
            if (self.stl_broadcast_enabled and 
                self.stl_data and 
                len(self.peers) > 1):
                
                try:
                    print(f"📐 Broadcasting STL data ({self.stl_data.filename}) to {len(self.peers)-1} peers")
                    self.broadcast_stl_data()
                except Exception as e:
                    print(f"❌ Error in STL broadcast: {e}")
            
            # If no peers, just wait quietly
            elif len(self.peers) <= 1:
                # Don't spam messages when no peers are connected
                pass
            elif not self.stl_broadcast_enabled or not self.stl_data:
                # STL not ready, wait quietly
                pass
    
    def stl_broadcast_thread_func(self):
        """Real threading function for STL broadcasting - runs independently of asyncio"""
        print("📐 STL broadcast thread started (real thread)")
        
        while self.running:
            time.sleep(15)  # Broadcast STL every 15 seconds (blocking sleep in thread)
            
            # Only broadcast if STL is enabled, data is loaded, and there are peers
            if (self.running and 
                self.socket and
                self.stl_broadcast_enabled and 
                self.stl_data and 
                len(self.peers) > 1):
                
                try:
                    print(f"📐 [Thread] Broadcasting STL data ({self.stl_data.filename}) to {len(self.peers)-1} peers")
                    self.broadcast_stl_data()
                except Exception as e:
                    print(f"❌ [Thread] Error in STL broadcast: {e}")
        
        print("📐 STL broadcast thread stopped")
    
    def get_peer_count(self) -> int:
        """Get the number of peers (excluding self)"""
        return len(self.peers) - 1
    
    def print_peer_list(self):
        """Print current peer list"""
        print(f"\n👥 Current Peers in Room ({len(self.peers)} total):")
        print(f"   🏠 Host: {self.peer.uuid[:8]}... (self)")
        
        for peer_uuid, peer in self.peers.items():
            if peer_uuid != self.peer.uuid:
                elapsed = time.time() - peer.join_time
                print(f"   🤝 Peer: {peer_uuid[:8]}... (joined {elapsed:.1f}s ago)")
    
    def load_stl_file(self, filepath: str) -> bool:
        """Load STL file and prepare for broadcasting"""
        if not STL_AVAILABLE:
            print("❌ Cannot load STL file: numpy-stl not installed")
            return False
        
        if not os.path.exists(filepath):
            print(f"❌ STL file not found: {filepath}")
            return False
        
        try:
            print(f"🔄 Loading STL file: {filepath}")
            
            # Load the STL mesh
            stl_mesh = mesh.Mesh.from_file(filepath)
            
            # Extract vertices and normals as flattened lists
            vertices = []
            normals = []
            
            for i in range(len(stl_mesh.vectors)):
                triangle = stl_mesh.vectors[i]
                normal = stl_mesh.normals[i]
                
                # Flatten triangle vertices: [x1,y1,z1, x2,y2,z2, x3,y3,z3]
                for vertex in triangle:
                    vertices.extend([float(vertex[0]), float(vertex[1]), float(vertex[2])])
                
                # Add normal for each vertex of the triangle (3 times)
                for _ in range(3):
                    normals.extend([float(normal[0]), float(normal[1]), float(normal[2])])
            
            # Calculate bounds
            all_vertices = np.array(stl_mesh.vectors).reshape(-1, 3)
            bounds = {
                "min_x": float(np.min(all_vertices[:, 0])),
                "max_x": float(np.max(all_vertices[:, 0])),
                "min_y": float(np.min(all_vertices[:, 1])),
                "max_y": float(np.max(all_vertices[:, 1])),
                "min_z": float(np.min(all_vertices[:, 2])),
                "max_z": float(np.max(all_vertices[:, 2]))
            }
            
            # Create STL data object
            self.stl_data = STLData(
                filename=os.path.basename(filepath),
                vertices=vertices,
                normals=normals,
                vertex_count=len(all_vertices),
                triangle_count=len(stl_mesh.vectors),
                bounds=bounds
            )
            
            print(f"✅ STL file loaded successfully:")
            print(f"   Filename: {self.stl_data.filename}")
            print(f"   Triangles: {self.stl_data.triangle_count}")
            print(f"   Vertices: {self.stl_data.vertex_count}")
            print(f"   Flattened vertices array length: {len(vertices)}")
            print(f"   Flattened normals array length: {len(normals)}")
            print(f"   Bounds: X({bounds['min_x']:.2f} to {bounds['max_x']:.2f}), Y({bounds['min_y']:.2f} to {bounds['max_y']:.2f}), Z({bounds['min_z']:.2f} to {bounds['max_z']:.2f})")
            
            return True
            
        except Exception as e:
            print(f"❌ Failed to load STL file: {e}")
            return False
    
    def enable_stl_broadcasting(self, enabled: bool = True):
        """Enable or disable STL data broadcasting"""
        self.stl_broadcast_enabled = enabled
        if enabled and self.stl_data:
            print(f"🔊 STL broadcasting enabled for {self.stl_data.filename}")
            # Start the STL broadcast thread if not already running
            if not self.stl_thread or not self.stl_thread.is_alive():
                self.stl_thread = threading.Thread(target=self.stl_broadcast_thread_func, daemon=True)
                self.stl_thread.start()
                print("🚀 STL broadcast thread started")
        elif enabled:
            print("⚠️ STL broadcasting enabled but no STL data loaded")
        else:
            print("🔇 STL broadcasting disabled")
    
    def broadcast_stl_data(self):
        """Broadcast STL data to all peers"""
        if not self.stl_data or not self.stl_broadcast_enabled:
            return
        
        if len(self.peers) <= 1:  # Only broadcast if there are other peers
            return
        
        # Create the message in the format Unity expects
        # Unity expects STL_Data directly in args, not wrapped in stl_data
        stl_dict = self.stl_data.to_dict()
        
        print(f"📐 Broadcasting STL data ({self.stl_data.filename}) to {len(self.peers)-1} peers")
        self.broadcast_to_peers("STL_DATA", stl_dict)


async def main():
    """Main function to run the room host"""
    print("🎯 Python Room Host for Ubiq Server")
    print("=" * 40)
    
    host = RoomHost()
    
    # Set up event listeners
    def on_room_created(room):
        if room.name == host.target_room_name:
            print(f"🏘️ Successfully Joined/Created Room!")
        else:
            print(f"🏘️ Room Ready!")
        print(f"   Name: {room.name}")
        print(f"   UUID: {room.uuid}")
        print(f"   Join Code: {room.joincode}")
        print(f"   Published: {room.publish}")
        print(f"\n💡 Other clients can join using code: {room.joincode}")
        print("⏳ Waiting for peers to join...")
    
    def on_rooms_discovered(rooms):
        if rooms:
            print(f"📋 Discovered {len(rooms)} existing room(s) on server")
        else:
            print("📋 No existing rooms found on server")
    
    def on_join_rejected(reason):
        print(f"❌ Room join was rejected: {reason}")
        print("🔄 Will try to create a new room instead...")
    
    def on_peer_joined(peer):
        print(f"\n🎉 New Peer Joined!")
        print(f"   UUID: {peer.uuid}")
        print(f"   Scene ID: {peer.sceneid}")
        print(f"   Network ID: {peer.clientid}")
        host.print_peer_list()
    
    def on_peer_left(peer):
        print(f"\n👋 Peer Left:")
        print(f"   UUID: {peer.uuid}")
        host.print_peer_list()
    
    def on_ping(session_id):
        # Don't print ping responses to reduce noise
        pass
    
    host.add_listener("OnJoinedRoom", on_room_created)
    host.add_listener("OnRoomsDiscovered", on_rooms_discovered)
    host.add_listener("OnJoinRejected", on_join_rejected)
    host.add_listener("OnPeerAdded", on_peer_joined)
    host.add_listener("OnPeerRemoved", on_peer_left)
    host.add_listener("OnPing", on_ping)
    
    # Connect and create room
    if await host.connect():
        # Start message processing loop
        message_task = asyncio.create_task(host.message_loop())
        broadcast_task = asyncio.create_task(host.broadcast_loop())
        # Note: STL broadcasting now uses real threading, started automatically when enabled
        
        try:
            # Discover and join/create room
            print("\n🔍 Checking for existing rooms...")
            room_name = "Python Host Room"
            if await host.join_or_create_room(room_name, publish=True):
                # Wait longer for room initialization
                print("⏳ Waiting for room initialization...")
                max_wait = 10.0  # Wait up to 10 seconds for room initialization  
                wait_time = 0.0
                while host.room.name == "Uninitialised Room Container" and wait_time < max_wait:
                    await asyncio.sleep(0.5)
                    wait_time += 0.5
                
                if host.room.name == "Uninitialised Room Container":
                    print("⚠️ Room initialization may have failed, but continuing...")
                else:
                    print(f"✅ Room fully initialized: {host.room.name}")
                
                # Load STL file if available
                stl_path = os.path.join(os.path.dirname(__file__), "stl", "cube.stl")
                if host.load_stl_file(stl_path):
                    host.enable_stl_broadcasting(True)
                    print("🎯 STL broadcasting enabled - 3D mesh data will be shared with peers")
                else:
                    print("⚠️ STL file not loaded - continuing without 3D mesh broadcasting")
                
                print(f"\n🎮 Room Host is running...")
                print("💬 Broadcasting messages to peers every 10 seconds")
                print("📐 STL broadcasting using real threading every 15 seconds")
                print("🏓 Sending keep-alive pings every 5 seconds")
                print("🔄 Press Ctrl+C to stop")
                
                # Keep running until interrupted
                while host.running:
                    await asyncio.sleep(1)
                    
                    # Print status every 30 seconds
                    if int(time.time()) % 30 == 0:
                        peer_count = host.get_peer_count()
                        room_status = "✅ Active" if host.room.uuid else "❌ Not Initialized"
                        connection_status = "✅ Connected" if host.is_connected() else "❌ Disconnected"
                        
                        print(f"\n📊 Status Report:")
                        print(f"   Room: '{host.room.name}' (code: {host.room.joincode}) - {room_status}")
                        print(f"   Connection: {connection_status}")
                        print(f"   Peers: {peer_count} connected")
                        print(f"   STL Broadcasting: {'✅ Enabled' if host.stl_broadcast_enabled else '❌ Disabled'}")
                        
                        if peer_count > 0:
                            host.print_peer_list()
                        elif host.room.uuid:
                            print("   💡 Waiting for Unity clients to connect using the join code above")
                        
                        # If disconnected, try to break the loop
                        if not host.is_connected():
                            print("❌ Lost connection to server, stopping...")
                            host.running = False
                            break
            else:
                print("❌ Failed to join or create room")
            
        except KeyboardInterrupt:
            print("\n\n🛑 Stopping Room Host...")
        except Exception as e:
            print(f"❌ Error: {e}")
        finally:
            host.disconnect()  # This will also stop the STL thread
            message_task.cancel()
            broadcast_task.cancel()
            try:
                await message_task
                await broadcast_task
            except asyncio.CancelledError:
                pass
            
            print("✅ Room Host stopped")


if __name__ == "__main__":
    asyncio.run(main())
