"""
TREND TRACKER 서버 실행 스크립트
eventlet + Flask-SocketIO를 통한 WebSocket 지원
"""
import os
import eventlet
eventlet.monkey_patch()

from server import app, socketio, _realtime_broadcast

port = int(os.environ.get("PORT", 5001))

print("=" * 50)
print("  TREND TRACKER API v4.0 (실시간 WebSocket)")
print("  http://0.0.0.0:%d" % port)
print("=" * 50)

socketio.start_background_task(_realtime_broadcast)
socketio.run(app, host="0.0.0.0", port=port, debug=False, log_output=True)
