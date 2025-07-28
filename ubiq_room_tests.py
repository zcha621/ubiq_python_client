#!/usr/bin/env python3
"""
Python Room Client for Ubiq Server
Based on the TypeScript RoomClient and test patterns
"""

import asyncio
import socket
import struct
import json
import uuid
import time
import math
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field


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


@dataclass
class RoomInfo:
    """Information about a room"""
    uuid: str = ""
    joincode: str = ""
    publish: bool = False
    name: str = "Uninitialised Room"
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
        json_bytes = data[12:12+(length-8)]
        
        # Parse NetworkId
        a, b = struct.unpack('<II', network_id_bytes)
        network_id = NetworkId(a, b)
        
        # Parse JSON
        json_data = json.loads(json_bytes.decode('utf-8'))
        
        return cls(network_id, json_data)


class RoomPeer:
    """Represents a peer in a room"""
    
    def __init__(self, peer_info: PeerInfo):
        self.uuid = peer_info.uuid
        self.sceneid = peer_info.sceneid
        self.clientid = peer_info.clientid
        self.properties: Dict[str, str] = {}
        
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


class RoomClient:
    """Python implementation of the TypeScript RoomClient"""
    
    def __init__(self, host: str = "localhost", port: int = 8009):
        self.host = host
        self.port = port
        self.socket: Optional[socket.socket] = None
        
        # Generate unique IDs
        self.scene_id = NetworkId.from_number(int(time.time() * 1000) % 0xFFFFFFFF)  # Scene ID
        self.network_id = NetworkId.create(self.scene_id, "RoomClient")  # Client ID
        self.server_network_id = NetworkId.from_number(1)  # Server ID is always 1
        
        # Initialize peer
        self.peer = RoomPeer(PeerInfo(
            uuid=str(uuid.uuid4()),
            sceneid=self.scene_id.b,  # Keep as number for internal use
            clientid=self.network_id
        ))
        
        # Initialize room and peers
        self.room = Room()
        self.peers: Dict[str, RoomPeer] = {self.peer.uuid: self.peer}
        
        # Event callbacks
        self.callbacks: Dict[str, List[Callable]] = {}
        
        print(f"Initialized RoomClient:")
        print(f"  Scene ID: {self.scene_id}")
        print(f"  Network ID: {self.network_id}")
        print(f"  Peer UUID: {self.peer.uuid}")
    
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
            self.socket.settimeout(10.0)  # Set timeout for connection
            self.socket.connect((self.host, self.port))
            self.socket.settimeout(None)  # Remove timeout after connection
            print(f"Connected to {self.host}:{self.port}")
            return True
        except Exception as e:
            print(f"Failed to connect: {e}")
            return False
    
    def is_connected(self) -> bool:
        """Check if socket is still connected"""
        if not self.socket:
            return False
        
        try:
            # Use SO_ERROR to check connection status (Windows compatible)
            error = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            return error == 0
        except socket.error:
            return False
    
    def disconnect(self):
        """Disconnect from the server"""
        if self.socket:
            self.socket.close()
            self.socket = None
            print("Disconnected from server")
    
    def send_server_message(self, message_type: str, args: Dict[str, Any]):
        """Send a message to the server"""
        if not self.socket:
            print("Not connected to server")
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
        
        print(f"Sending {message_type}: {args}")
        print(f"Message content: {json_string}")
        try:
            sent = self.socket.send(data)
            if sent != len(data):
                print(f"Warning: Only sent {sent} of {len(data)} bytes")
                return False
            return True
        except Exception as e:
            print(f"Failed to send message: {e}")
            self.disconnect()  # Disconnect on send error
            return False
    
    def receive_message(self) -> Optional[Message]:
        """Receive a message from the server"""
        if not self.socket:
            return None
        
        try:
            # Use select to check if data is available (Windows compatible)
            import select
            ready, _, _ = select.select([self.socket], [], [], 0.1)  # 100ms timeout
            if not ready:
                return None
            
            # Read length first (4 bytes)
            length_data = b''
            while len(length_data) < 4:
                chunk = self.socket.recv(4 - len(length_data))
                if not chunk:
                    print("Connection closed while reading length")
                    return None
                length_data += chunk
            
            length = struct.unpack('<I', length_data)[0]
            
            # Validate length
            if length <= 0 or length > 1024 * 1024:  # Max 1MB message
                print(f"Invalid message length: {length}")
                return None
            
            # Read the rest of the message (length bytes total, including the 8-byte NetworkId)
            remaining = length
            data = bytearray(length_data)
            
            while remaining > 0:
                chunk = self.socket.recv(min(remaining, 4096))
                if not chunk:
                    print("Connection closed while reading message body")
                    return None
                data.extend(chunk)
                remaining -= len(chunk)
            
            return Message.from_bytes(bytes(data))
        
        except Exception as e:
            print(f"Failed to receive message: {e}")
            return None
    
    def process_message(self, message: Message):
        """Process a received message"""
        try:
            data = message.data
            message_type = data.get("type")
            args = json.loads(data.get("args", "{}"))
            
            print(f"Received {message_type}: {args}")
            
            if message_type == "SetRoom":
                self.set_room_info(args["room"])
                self.emit("OnJoinedRoom", self.room)
            
            elif message_type == "Rejected":
                self.emit("OnJoinRejected", args["reason"])
            
            elif message_type == "Rooms":
                self.emit("OnRooms", args)
            
            elif message_type == "PeerAdded":
                peer_data = args["peer"]
                sceneid_data = peer_data["sceneid"]
                
                # Handle sceneid as NetworkId object
                if isinstance(sceneid_data, dict):
                    sceneid = sceneid_data.get("b", 0)  # Use b component as the scene ID number
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
            
            elif message_type == "PeerRemoved":
                peer_uuid = args["uuid"]
                if peer_uuid in self.peers:
                    peer = self.peers[peer_uuid]
                    del self.peers[peer_uuid]
                    self.emit("OnPeerRemoved", peer)
            
            elif message_type == "Ping":
                self.emit("OnPing", args["sessionId"])
            
            else:
                print(f"Unknown message type: {message_type}")
        
        except Exception as e:
            print(f"Error processing message: {e}")
    
    def set_room_info(self, room_info: Dict[str, Any]):
        """Set room information"""
        self.room.uuid = room_info.get("uuid", "")
        self.room.joincode = room_info.get("joincode", "")
        self.room.publish = room_info.get("publish", False)
        self.room.name = room_info.get("name", "")
        
        # Set properties
        keys = room_info.get("keys", [])
        values = room_info.get("values", [])
        self.room.properties = dict(zip(keys, values))
    
    def get_peer_info(self) -> Dict[str, Any]:
        """Get peer information for sending to server"""
        return {
            "uuid": self.peer.uuid,
            "sceneid": {"a": self.scene_id.a, "b": self.scene_id.b},  # Send sceneid as NetworkId object
            "clientid": {"a": self.network_id.a, "b": self.network_id.b},
            "keys": list(self.peer.properties.keys()),
            "values": list(self.peer.properties.values())
        }
    
    def ping(self):
        """Send a ping to the server"""
        self.send_server_message("Ping", {
            "clientid": {"a": self.network_id.a, "b": self.network_id.b}
        })
    
    def discover_rooms(self):
        """Discover available rooms"""
        self.send_server_message("DiscoverRooms", {
            "clientid": {"a": self.network_id.a, "b": self.network_id.b},
            "joincode": ""
        })
    
    def join(self, identifier: Optional[str] = None, publish: bool = False, name: str = "My Room"):
        """Join a room by UUID, join code, or create a new room"""
        args = {
            "joincode": "",
            "name": name,
            "publish": publish,
            "peer": self.get_peer_info()
        }
        
        if identifier is None:
            # Create new room with default name
            pass
        elif len(identifier) == 3:
            # Join by join code
            args["joincode"] = identifier
        elif len(identifier) == 36 and '-' in identifier:
            # Join by UUID
            args["uuid"] = identifier
        else:
            # Create new room with custom name
            args["name"] = identifier
        
        self.send_server_message("Join", args)
    
    async def message_loop(self):
        """Main message processing loop"""
        while self.socket:
            try:
                message = self.receive_message()
                if message:
                    self.process_message(message)
                else:
                    await asyncio.sleep(0.1)
            except Exception as e:
                print(f"Error in message loop: {e}")
                break


async def test_room_client():
    """Test the room client functionality"""
    client = RoomClient()
    
    # Set up event listeners
    client.add_listener("OnPing", lambda session_id: print(f"✓ Ping response: {session_id}"))
    client.add_listener("OnJoinedRoom", lambda room: print(f"✓ Joined room: {room.name} (UUID: {room.uuid}, Join Code: {room.joincode})"))
    client.add_listener("OnRooms", lambda rooms: print(f"✓ Available rooms: {rooms}"))
    client.add_listener("OnPeerAdded", lambda peer: print(f"✓ Peer added: {peer.uuid}"))
    client.add_listener("OnPeerRemoved", lambda peer: print(f"✓ Peer removed: {peer.uuid}"))
    client.add_listener("OnJoinRejected", lambda reason: print(f"✗ Join rejected: {reason}"))
    
    # Connect to server
    if await client.connect():
        # Start message processing loop
        message_task = asyncio.create_task(client.message_loop())
        
        try:
            # Test ping
            print("\n=== Testing Ping ===")
            if client.send_server_message("Ping", {"clientid": {"a": client.network_id.a, "b": client.network_id.b}}):
                await asyncio.sleep(2)
            
            # Check if still connected
            if not client.is_connected():
                print("Lost connection after ping")
                return
            
            # Test room discovery
            print("\n=== Discovering Rooms ===")
            client.discover_rooms()
            await asyncio.sleep(2)
            
            # Check if still connected
            if not client.is_connected():
                print("Lost connection after discover rooms")
                return
            
            # Test creating a room
            print("\n=== Creating Room ===")
            client.join(None, False, "Python Test Room")
            await asyncio.sleep(3)
            
            # Test creating another room with UUID
            if client.is_connected():
                print("\n=== Creating Room with UUID ===")
                test_uuid = str(uuid.uuid4())
                client.join(test_uuid)
                await asyncio.sleep(3)
            
            print("\n=== Test completed ===")
            
        except KeyboardInterrupt:
            print("\nStopping...")
        except Exception as e:
            print(f"Test error: {e}")
        finally:
            message_task.cancel()
            try:
                await message_task
            except asyncio.CancelledError:
                pass
            client.disconnect()


if __name__ == "__main__":
    print("Python Room Client for Ubiq Server")
    print("==================================")
    asyncio.run(test_room_client())
