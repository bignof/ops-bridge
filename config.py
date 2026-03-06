import os
import socket
import sys
import logging

from dotenv import load_dotenv
load_dotenv()

WS_URL             = os.getenv('WS_URL', '')
AGENT_ID           = os.getenv('AGENT_ID', socket.gethostname())
TOKEN              = os.getenv('TOKEN', '')
RECONNECT_DELAY    = int(os.getenv('RECONNECT_DELAY', '5'))
HEARTBEAT_INTERVAL = int(os.getenv('HEARTBEAT_INTERVAL', '30'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

if not WS_URL:
    sys.exit("ERROR: WS_URL is not set. Example: ws://192.168.1.100:8080/ws/agent")
if not TOKEN:
    sys.exit("ERROR: TOKEN is not set.")
