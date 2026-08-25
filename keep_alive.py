from flask import Flask
import threading
import os

app = Flask(__name__)

@app.route('/')
def home():
    # هذه هي الصفحة التي سيزورها موقع UptimeRobot بكل شفافية
    return "Bot is Alive and Running!"

def run():
    # Render يحدد البورت (Port) تلقائياً، وإذا لم يجده سيستخدم 8080
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    # تشغيل السيرفر في مسار جانبي (Thread) لكي لا يعطل عمل البوت
    t = threading.Thread(target=run)
    t.start()

