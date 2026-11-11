import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cloud_http import CloudHandler
class handler(CloudHandler):
    pass
