"""Start or reopen this local workspace, without installing packages."""
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT=Path(__file__).resolve().parent
URL='http://127.0.0.1:8765'

def ready():
    try:
        with urllib.request.urlopen(URL+'/api/state',timeout=1) as response:
            return json.load(response).get('version')=='0.1.0'
    except Exception: return False

if __name__=='__main__':
    if ready():
        webbrowser.open(URL)
        print('工作台已在运行：'+URL)
        sys.exit(0)
    process=subprocess.Popen([sys.executable,str(ROOT/'server.py'),'--port','8765'],cwd=str(ROOT))
    try:
        for _ in range(50):
            if process.poll() is not None: raise RuntimeError('启动失败，请检查 8765 端口是否被其他程序占用。')
            if ready(): break
            time.sleep(.1)
        else: raise RuntimeError('启动超时，请在终端运行 python3 server.py 查看原因。')
        webbrowser.open(URL)
        print('\n寻源工作台已打开。保留此窗口以继续运行；按 Control+C 停止。\n')
        process.wait()
    except KeyboardInterrupt:
        process.terminate(); process.wait()
        print('\n工作台已停止，数据已保存在 data/supplier.db。')
    except Exception as error:
        process.terminate(); process.wait()
        print(error)
        sys.exit(1)
