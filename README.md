# Introduction
This repo provides python test codes to connected to the ubiq server, create room and broadcast message over all peers in the room created by python.

# room_client.py
- Connect to the ubiq server 
- Create a room named python
- Broadcast the stl model under stl folder to all the connected peers
  - Manually create the stl folder and put the stl model into the created folder
  - Change the stl file name at: stl_path = os.path.join(os.path.dirname(__file__), "stl", "cube.stl")
- The ubiq server, unity client to receive and visualize the stl model in unity can be found: https://github.com/zcha621/ubiq_xr_remote_collaboration/tree/master
# ubiq_room_tests.py
- a Python version simulation of rooms.tests.ts under https://github.com/zcha621/ubiq_xr_remote_collaboration/tree/master/Node/tests
