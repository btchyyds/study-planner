import sqlite3, jwt, json, time, hashlib, os, datetime, threading, queue, csv, io, sys
from functools import wraps
from flask import Flask, request, jsonify, g, send_from_directory, Response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from urllib.parse import urlparse

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)
SECRET_KEY = os.environ.get('JWT_SECRET')
DB_PATH = os.environ.get('DB_PATH', 'study.db')
POINTS_RATIO_MINUTES = 60

SSE_CLIENTS = {}
SSE_LOCK = threading.Lock()

def validate_config():
    if not SECRET_KEY:
        print('ERROR: 环境变量 JWT_SECRET 未配置，请设置 ≥32 字符的随机密钥')
        sys.exit(1)
    if len(SECRET_KEY) < 32:
        print('ERROR: JWT_SECRET 长度不足 32 字符，当前长度 %d，请使用更强的密钥' % len(SECRET_KEY))
        sys.exit(1)
    if not DB_PATH and not os.environ.get('DATABASE_URL'):
        print('ERROR: DB_PATH 或 DATABASE_URL 至少需要配置一个')
        sys.exit(1)

validate_config()

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS tasks(
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        duration INTEGER DEFAULT 30,
        priority TEXT DEFAULT 'mid',
        tag TEXT,
        note TEXT,
        type TEXT DEFAULT 'once',
        repeat_rule TEXT DEFAULT 'daily',
        repeat_paused INTEGER DEFAULT 0,
        custom_days TEXT,
        remind_time TEXT,
        remind_on INTEGER DEFAULT 1,
        remind_interval INTEGER DEFAULT 15,
        remind_mode TEXT DEFAULT 'notification',
        date TEXT,
        status TEXT DEFAULT 'incomplete',
        actual_duration INTEGER,
        completed_at TEXT,
        review_note TEXT,
        target_date TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS checkins(
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        task_id TEXT NOT NULL,
        checkin_date TEXT NOT NULL,
        status TEXT NOT NULL,
        actual_duration INTEGER DEFAULT 0,
        review_note TEXT,
        completed_at TEXT,
        points_settled INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS point_accounts(
        user_id INTEGER PRIMARY KEY,
        total_earned INTEGER DEFAULT 0,
        total_spent INTEGER DEFAULT 0,
        pending_minutes INTEGER DEFAULT 0,
        streak_days INTEGER DEFAULT 0,
        max_streak_days INTEGER DEFAULT 0,
        last_checkin_date TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS points_log(
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        amount INTEGER NOT NULL,
        desc TEXT,
        source_task_id TEXT,
        source_duration INTEGER,
        redeem_item_id TEXT,
        time TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS shop_items(
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        points INTEGER NOT NULL,
        link TEXT,
        image TEXT,
        hidden INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS redeem_history(
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        item_id TEXT,
        item_name TEXT,
        points INTEGER NOT NULL,
        item_link TEXT,
        item_image TEXT,
        time TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS goals(
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        tag TEXT NOT NULL,
        target INTEGER DEFAULT 5,
        period TEXT DEFAULT 'week',
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS settings(
        user_id INTEGER PRIMARY KEY,
        dnd_start TEXT DEFAULT '22:00',
        dnd_end TEXT DEFAULT '07:00',
        remind_interval INTEGER DEFAULT 15,
        remind_mode TEXT DEFAULT 'notification',
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS weekly_summaries(
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        week_start TEXT NOT NULL,
        total_minutes INTEGER DEFAULT 0,
        summary_text TEXT,
        missed_tasks TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(user_id, week_start),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    for col, ddl in [
        ('remind_interval', "ALTER TABLE tasks ADD COLUMN remind_interval INTEGER DEFAULT 15"),
        ('remind_mode', "ALTER TABLE tasks ADD COLUMN remind_mode TEXT DEFAULT 'notification'"),
        ('target_date', "ALTER TABLE tasks ADD COLUMN target_date TEXT"),
    ]:
        try: db.execute(ddl)
        except: pass
    try: db.execute("ALTER TABLE redeem_history ADD COLUMN item_image TEXT")
    except: pass
    for col, ddl in [
        ('source_task_id', "ALTER TABLE points_log ADD COLUMN source_task_id TEXT"),
        ('source_duration', "ALTER TABLE points_log ADD COLUMN source_duration INTEGER"),
        ('redeem_item_id', "ALTER TABLE points_log ADD COLUMN redeem_item_id TEXT"),
    ]:
        try: db.execute(ddl)
        except: pass
    try:
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_checkins_task_date ON checkins(task_id, checkin_date)")
    except: pass
    admin = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if not admin:
        db.execute("INSERT INTO users(username,password,is_admin) VALUES(?,?,1)",
                   ('admin', generate_password_hash('admin123')))
    db.commit()
    db.close()

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization','').replace('Bearer ','')
        if not token:
            return jsonify({'error':'未登录'}), 401
        try:
            data = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
            g.user_id = data['user_id']
            g.is_admin = data['is_admin']
            g.username = data['username']
        except:
            return jsonify({'error':'token无效'}), 401
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization','').replace('Bearer ','')
        if not token:
            return jsonify({'error':'未登录'}), 401
        try:
            data = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
            if not data['is_admin']:
                return jsonify({'error':'无管理员权限'}), 403
            g.user_id = data['user_id']
            g.is_admin = 1
            g.username = data['username']
        except:
            return jsonify({'error':'token无效'}), 401
        return f(*args, **kwargs)
    return decorated

def row_to_dict(row):
    return {k: row[k] for k in row.keys()} if row else None

def gen_id():
    return str(int(time.time()*1000)) + hashlib.md5(str(time.time()).encode()).hexdigest()[:6]

def validate_url(url):
    if not url: return True
    try:
        r = urlparse(url)
        return r.scheme in ('http','https') and bool(r.netloc)
    except: return False

# ============ Auth ============
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username','').strip()
    password = data.get('password','')
    if not username or not password:
        return jsonify({'error':'用户名和密码不能为空'}), 400
    if len(password) < 6:
        return jsonify({'error':'密码至少6位'}), 400
    db = get_db()
    if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
        return jsonify({'error':'用户名已存在'}), 400
    cur = db.execute("INSERT INTO users(username,password) VALUES(?,?)",
               (username, generate_password_hash(password)))
    uid = cur.lastrowid
    db.execute("INSERT OR IGNORE INTO settings(user_id) VALUES(?)", (uid,))
    db.execute("INSERT OR IGNORE INTO point_accounts(user_id) VALUES(?)", (uid,))
    db.commit()
    return jsonify({'msg':'注册成功'})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username','').strip()
    password = data.get('password','')
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not user or not check_password_hash(user['password'], password):
        return jsonify({'error':'用户名或密码错误'}), 400
    token = jwt.encode({
        'user_id': user['id'], 'is_admin': user['is_admin'],
        'username': user['username'], 'exp': int(time.time())+86400*30
    }, SECRET_KEY, algorithm='HS256')
    return jsonify({'token':token,'is_admin':user['is_admin'],'username':user['username']})

# ============ Tasks ============
@app.route('/api/tasks', methods=['GET'])
@token_required
def get_tasks():
    db = get_db()
    rows = db.execute("SELECT * FROM tasks WHERE user_id=? ORDER BY created_at DESC", (g.user_id,)).fetchall()
    return jsonify([row_to_dict(r) for r in rows])

@app.route('/api/tasks', methods=['POST'])
@token_required
def add_task():
    data = request.json
    tid = gen_id()
    db = get_db()
    db.execute("""INSERT INTO tasks(id,user_id,name,duration,priority,tag,note,type,repeat_rule,repeat_paused,custom_days,remind_time,remind_on,date,status)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tid,g.user_id,data.get('name'),data.get('duration',30),data.get('priority','mid'),
         data.get('tag'),data.get('note'),data.get('type','once'),data.get('repeatRule','daily'),
         1 if data.get('repeatPaused') else 0,json.dumps(data.get('customDays',[])),
         data.get('remindTime'),1 if data.get('remindOn',True) else 0,data.get('date'),'incomplete'))
    db.commit()
    return jsonify({'id':tid})

@app.route('/api/tasks/<tid>', methods=['PUT'])
@token_required
def update_task(tid):
    data = request.json
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (tid,g.user_id)).fetchone()
    if not task: return jsonify({'error':'任务不存在'}), 404
    old_dur = task['actual_duration'] or 0
    fields = ['name','duration','priority','tag','note','type','repeat_rule','repeat_paused','remind_time','remind_on','date','status','actual_duration','completed_at','review_note']
    updates = []
    vals = []
    mapping = {'repeatRule':'repeat_rule','repeatPaused':'repeat_paused','remindTime':'remind_time','remindOn':'remind_on','actualDuration':'actual_duration','completedAt':'completed_at','reviewNote':'review_note'}
    for k,v in data.items():
        fk = mapping.get(k,k)
        if fk in fields:
            if fk in ('repeat_paused','remind_on'):
                v = 1 if v else 0
            if k == 'customDays':
                fk = 'custom_days'; v = json.dumps(v)
            updates.append(f"{fk}=?")
            vals.append(v)
    if updates:
        vals.append(tid)
        db.execute(f"UPDATE tasks SET {','.join(updates)} WHERE id=?", vals)
        db.commit()
    new_dur = data.get('actualDuration', old_dur)
    if 'actualDuration' in data and new_dur != old_dur:
        recalc_points(g.user_id, task['name'], old_dur, new_dur, db)
        db.commit()
    return jsonify({'msg':'ok'})

@app.route('/api/tasks/<tid>', methods=['DELETE'])
@token_required
def delete_task(tid):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (tid,g.user_id)).fetchone()
    if not task: return jsonify({'error':'任务不存在'}), 404
    ck = db.execute("SELECT * FROM checkins WHERE task_id=? AND points_settled=1", (tid,)).fetchall()
    rollback = 0
    for c in ck:
        pts = c['actual_duration'] // 60
        if pts > 0:
            rollback += pts
            db.execute("INSERT INTO points_log(id,user_id,type,amount,desc,source_task_id,source_duration) VALUES(?,?,?,?,?,?,?)",
                       (gen_id(),g.user_id,'spend',pts,f"删除任务回退: {task['name']}",tid,c['actual_duration']))
    if rollback > 0:
        db.execute("UPDATE point_accounts SET total_earned=total_earned-? WHERE user_id=?", (rollback,g.user_id))
    db.execute("DELETE FROM checkins WHERE task_id=?", (tid,))
    db.execute("DELETE FROM tasks WHERE id=?", (tid,))
    db.commit()
    return jsonify({'msg':'ok','rollback':rollback})

def recalc_points(user_id, task_name, old_dur, new_dur, db):
    old_pts = old_dur // 60
    new_pts = new_dur // 60
    diff = new_pts - old_pts
    if diff > 0:
        db.execute("INSERT INTO points_log(id,user_id,type,amount,desc) VALUES(?,?,?,?)",
                   (gen_id(),user_id,'earn',diff,f"学习任务: {task_name}"))
        db.execute("UPDATE point_accounts SET total_earned=total_earned+? WHERE user_id=?", (diff,user_id))
    elif diff < 0:
        db.execute("INSERT INTO points_log(id,user_id,type,amount,desc) VALUES(?,?,?,?)",
                   (gen_id(),user_id,'spend',-diff,f"修改时长回退: {task_name}"))
        db.execute("UPDATE point_accounts SET total_earned=total_earned+? WHERE user_id=?", (diff,user_id))

# ============ Points ============
def ensure_point_account(user_id, db):
    db.execute("INSERT OR IGNORE INTO point_accounts(user_id) VALUES(?)", (user_id,))
    return db.execute("SELECT * FROM point_accounts WHERE user_id=?", (user_id,)).fetchone()

@app.route('/api/points/account', methods=['GET'])
@token_required
def points_account():
    db = get_db()
    acc = ensure_point_account(g.user_id, db)
    db.commit()
    return jsonify({
        'total_earned': acc['total_earned'],
        'total_spent': acc['total_spent'],
        'available': acc['total_earned'] - acc['total_spent'],
        'pending_minutes': acc['pending_minutes'],
        'minutes_to_next_point': POINTS_RATIO_MINUTES - acc['pending_minutes'] if acc['pending_minutes'] < POINTS_RATIO_MINUTES else 0
    })

@app.route('/api/points', methods=['GET'])
@token_required
def get_points():
    db = get_db()
    acc = ensure_point_account(g.user_id, db)
    db.commit()
    rows = db.execute("SELECT * FROM points_log WHERE user_id=? ORDER BY time DESC", (g.user_id,)).fetchall()
    return jsonify({'total':acc['total_earned'],'spent':acc['total_spent'],
                    'available':acc['total_earned']-acc['total_spent'],
                    'pending_minutes':acc['pending_minutes'],
                    'log':[row_to_dict(r) for r in rows]})

# ============ Shop ============
@app.route('/api/shop', methods=['GET'])
@token_required
def get_shop():
    db = get_db()
    rows = db.execute("SELECT * FROM shop_items WHERE hidden=0 ORDER BY created_at DESC").fetchall()
    redeemed = db.execute("SELECT item_id FROM redeem_history WHERE user_id=?", (g.user_id,)).fetchall()
    redeemed_ids = [r['item_id'] for r in redeemed]
    items = []
    for r in rows:
        d = row_to_dict(r)
        d['custom_days'] = None
        d['redeemed'] = r['id'] in redeemed_ids
        items.append(d)
    return jsonify(items)

@app.route('/api/shop', methods=['POST'])
@admin_required
def add_shop_item():
    data = request.json
    link = data.get('link','')
    image = data.get('image','')
    if not validate_url(link) or not validate_url(image):
        return jsonify({'error':'请输入合法的 URL'}), 400
    sid = gen_id()
    db = get_db()
    db.execute("INSERT INTO shop_items(id,name,points,link,image) VALUES(?,?,?,?,?)",
               (sid,data.get('name'),data.get('points',10),link,image))
    db.commit()
    return jsonify({'id':sid})

@app.route('/api/shop/<sid>', methods=['PUT'])
@admin_required
def update_shop_item(sid):
    data = request.json
    link = data.get('link','')
    image = data.get('image','')
    if not validate_url(link) or not validate_url(image):
        return jsonify({'error':'请输入合法的 URL'}), 400
    db = get_db()
    db.execute("UPDATE shop_items SET name=?,points=?,link=?,image=? WHERE id=?",
               (data.get('name'),data.get('points'),link,image,sid))
    db.commit()
    return jsonify({'msg':'ok'})

@app.route('/api/shop/<sid>', methods=['DELETE'])
@admin_required
def delete_shop_item(sid):
    db = get_db()
    db.execute("UPDATE shop_items SET hidden=1 WHERE id=?", (sid,))
    db.commit()
    return jsonify({'msg':'ok'})

@app.route('/api/shop/redeem', methods=['POST'])
@token_required
def redeem_item():
    data = request.json
    sid = data.get('itemId')
    db = get_db()
    item = db.execute("SELECT * FROM shop_items WHERE id=? AND hidden=0", (sid,)).fetchone()
    if not item: return jsonify({'error':'商品不存在'}), 404
    acc = ensure_point_account(g.user_id, db)
    available = acc['total_earned'] - acc['total_spent']
    if available < item['points']:
        return jsonify({'error':'积分不足无法兑换'}), 400
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE point_accounts SET total_spent=total_spent+? WHERE user_id=?",
                   (item['points'], g.user_id))
        db.execute("INSERT INTO points_log(id,user_id,type,amount,desc,redeem_item_id) VALUES(?,?,?,?,?,?)",
                   (gen_id(),g.user_id,'spend',item['points'],f"兑换: {item['name']}",sid))
        db.execute("INSERT INTO redeem_history(id,user_id,item_id,item_name,points,item_link,item_image) VALUES(?,?,?,?,?,?,?)",
                   (gen_id(),g.user_id,sid,item['name'],item['points'],item['link'],item['image']))
        db.execute("COMMIT")
    except Exception as e:
        db.execute("ROLLBACK")
        return jsonify({'error':'兑换失败,请重试'}), 500
    db.commit()
    return jsonify({'msg':'兑换成功','consumed':item['points'],'available':available-item['points']})

@app.route('/api/redeem/history', methods=['GET'])
@token_required
def redeem_history():
    db = get_db()
    rows = db.execute("SELECT * FROM redeem_history WHERE user_id=? ORDER BY time DESC", (g.user_id,)).fetchall()
    return jsonify([row_to_dict(r) for r in rows])

# ============ Goals ============
@app.route('/api/goals', methods=['GET'])
@token_required
def get_goals():
    db = get_db()
    rows = db.execute("SELECT * FROM goals WHERE user_id=?", (g.user_id,)).fetchall()
    return jsonify([row_to_dict(r) for r in rows])

@app.route('/api/goals', methods=['POST'])
@token_required
def add_goal():
    data = request.json
    gid = gen_id()
    db = get_db()
    db.execute("INSERT INTO goals(id,user_id,name,tag,target) VALUES(?,?,?,?,?)",
               (gid,g.user_id,data.get('name'),data.get('tag'),data.get('target',5)))
    db.commit()
    return jsonify({'id':gid})

@app.route('/api/goals/<gid>', methods=['DELETE'])
@token_required
def delete_goal(gid):
    db = get_db()
    db.execute("DELETE FROM goals WHERE id=? AND user_id=?", (gid,g.user_id))
    db.commit()
    return jsonify({'msg':'ok'})

# ============ Settings ============
@app.route('/api/settings', methods=['GET','PUT'])
@token_required
def settings():
    db = get_db()
    if request.method == 'GET':
        row = db.execute("SELECT * FROM settings WHERE user_id=?", (g.user_id,)).fetchone()
        if not row:
            db.execute("INSERT INTO settings(user_id) VALUES(?)", (g.user_id,))
            db.commit()
            row = db.execute("SELECT * FROM settings WHERE user_id=?", (g.user_id,)).fetchone()
        return jsonify(row_to_dict(row))
    else:
        data = request.json
        db.execute("""INSERT OR REPLACE INTO settings(user_id,dnd_start,dnd_end,remind_interval,remind_mode)
        VALUES(?,?,?,?,?)""",
            (g.user_id,data.get('dnd_start','22:00'),data.get('dnd_end','07:00'),
             data.get('remind_interval',15),data.get('remind_mode','notification')))
        db.commit()
        return jsonify({'msg':'ok'})

# ============ Admin ============
@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_users():
    db = get_db()
    rows = db.execute("SELECT id,username,is_admin,created_at FROM users WHERE is_admin=0 ORDER BY created_at DESC").fetchall()
    result = []
    for r in rows:
        earned = db.execute("SELECT COALESCE(SUM(amount),0) as s FROM points_log WHERE user_id=? AND type='earn'", (r['id'],)).fetchone()['s']
        spent = db.execute("SELECT COALESCE(SUM(amount),0) as s FROM points_log WHERE user_id=? AND type='spend'", (r['id'],)).fetchone()['s']
        task_count = db.execute("SELECT COUNT(*) as c FROM tasks WHERE user_id=?", (r['id'],)).fetchone()['c']
        result.append({
            'id':r['id'],'username':r['username'],'created_at':r['created_at'],
            'points':earned-spent,'task_count':task_count
        })
    return jsonify(result)

@app.route('/api/admin/users/<int:uid>', methods=['GET'])
@admin_required
def admin_user_detail(uid):
    db = get_db()
    user = db.execute("SELECT id,username,created_at FROM users WHERE id=?", (uid,)).fetchone()
    if not user: return jsonify({'error':'用户不存在'}), 404
    tasks = db.execute("SELECT * FROM tasks WHERE user_id=? ORDER BY date DESC", (uid,)).fetchall()
    points = db.execute("SELECT * FROM points_log WHERE user_id=? ORDER BY time DESC", (uid,)).fetchall()
    redeems = db.execute("SELECT * FROM redeem_history WHERE user_id=? ORDER BY time DESC", (uid,)).fetchall()
    earned = db.execute("SELECT COALESCE(SUM(amount),0) as s FROM points_log WHERE user_id=? AND type='earn'", (uid,)).fetchone()['s']
    spent = db.execute("SELECT COALESCE(SUM(amount),0) as s FROM points_log WHERE user_id=? AND type='spend'", (uid,)).fetchone()['s']
    return jsonify({
        'user':row_to_dict(user),
        'tasks':[row_to_dict(r) for r in tasks],
        'points_log':[row_to_dict(r) for r in points],
        'redeem_history':[row_to_dict(r) for r in redeems],
        'points_total':earned,'points_spent':spent,'points_available':earned-spent
    })

@app.route('/api/admin/shop', methods=['GET'])
@admin_required
def admin_shop_all():
    db = get_db()
    rows = db.execute("SELECT * FROM shop_items ORDER BY created_at DESC").fetchall()
    return jsonify([row_to_dict(r) for r in rows])

# ============ Checkin & Points Settlement ============
def is_in_dnd(dnd_start, dnd_end, now=None):
    if not dnd_start or not dnd_end: return False
    now = now or datetime.datetime.now()
    cur = now.hour * 60 + now.minute
    sh, sm = map(int, dnd_start.split(':'))
    eh, em = map(int, dnd_end.split(':'))
    s, e = sh*60+sm, eh*60+em
    if s <= e: return s <= cur < e
    return cur >= s or cur < e

@app.route('/api/tasks/checkin', methods=['POST'])
@token_required
def checkin():
    data = request.json
    tid = data.get('task_id') or data.get('taskId')
    status = data.get('status','completed')
    actual_duration = int(data.get('actual_duration', data.get('actualDuration', 0)) or 0)
    review_note = data.get('review_note', data.get('reviewNote',''))
    today = datetime.date.today().isoformat()
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (tid,g.user_id)).fetchone()
    if not task: return jsonify({'error':'任务不存在'}), 404
    acc = ensure_point_account(g.user_id, db)
    old_ck = db.execute("SELECT * FROM checkins WHERE task_id=? AND checkin_date=?", (tid,today)).fetchone()
    rollback_minutes = 0
    if old_ck and old_ck['points_settled']:
        old_pts = old_ck['actual_duration'] // POINTS_RATIO_MINUTES
        if old_pts > 0:
            db.execute("UPDATE point_accounts SET total_earned=total_earned-? WHERE user_id=?", (old_pts,g.user_id))
            db.execute("INSERT INTO points_log(id,user_id,type,amount,desc,source_task_id,source_duration) VALUES(?,?,?,?,?,?,?)",
                       (gen_id(),g.user_id,'spend',old_pts,f"重算回退: {task['name']}",tid,old_ck['actual_duration']))
        rollback_minutes = old_ck['actual_duration'] % POINTS_RATIO_MINUTES
    new_pending = acc['pending_minutes'] - rollback_minutes + actual_duration
    if new_pending < 0: new_pending = 0
    points_earned = new_pending // POINTS_RATIO_MINUTES
    remain_minutes = new_pending % POINTS_RATIO_MINUTES
    if points_earned > 0:
        db.execute("UPDATE point_accounts SET total_earned=total_earned+?, pending_minutes=? WHERE user_id=?",
                   (points_earned, remain_minutes, g.user_id))
        db.execute("INSERT INTO points_log(id,user_id,type,amount,desc,source_task_id,source_duration) VALUES(?,?,?,?,?,?,?)",
                   (gen_id(),g.user_id,'earn',points_earned,f"学习任务: {task['name']}",tid,actual_duration))
    else:
        db.execute("UPDATE point_accounts SET pending_minutes=? WHERE user_id=?", (remain_minutes,g.user_id))
    cid = old_ck['id'] if old_ck else gen_id()
    if old_ck:
        db.execute("UPDATE checkins SET status=?,actual_duration=?,review_note=?,completed_at=?,points_settled=? WHERE id=?",
                   (status,actual_duration,review_note,datetime.datetime.now().isoformat(),1 if points_earned>0 or actual_duration>=POINTS_RATIO_MINUTES else 0,cid))
    else:
        db.execute("INSERT INTO checkins(id,user_id,task_id,checkin_date,status,actual_duration,review_note,completed_at,points_settled) VALUES(?,?,?,?,?,?,?,?,?)",
                   (cid,g.user_id,tid,today,status,actual_duration,review_note,datetime.datetime.now().isoformat(),1 if points_earned>0 or actual_duration>=POINTS_RATIO_MINUTES else 0))
    db.execute("UPDATE tasks SET status=?,actual_duration=?,completed_at=?,review_note=? WHERE id=?",
               (status,actual_duration,datetime.datetime.now().isoformat(),review_note,tid))
    if status in ('completed','partial'):
        last_date = acc['last_checkin_date']
        if last_date != today:
            yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
            new_streak = (acc['streak_days'] + 1) if last_date == yesterday else 1
            db.execute("UPDATE point_accounts SET streak_days=?, max_streak_days=MAX(max_streak_days,?), last_checkin_date=? WHERE user_id=?",
                       (new_streak, new_streak, today, g.user_id))
    db.commit()
    return jsonify({'msg':'打卡成功','points_earned':points_earned,
                    'pending_minutes':remain_minutes,
                    'minutes_to_next_point':POINTS_RATIO_MINUTES-remain_minutes if remain_minutes<POINTS_RATIO_MINUTES else 0})

# ============ Today Tasks & Cycle Expander ============
def expand_cycle_tasks(user_id, db, target_date):
    tasks = db.execute("SELECT * FROM tasks WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    result = []
    wd = target_date.weekday()
    for t in tasks:
        d = row_to_dict(t)
        d['custom_days'] = json.loads(t['custom_days']) if t['custom_days'] else []
        if t['type'] == 'once':
            td = t['target_date'] or t['date']
            if td == target_date.isoformat():
                d['is_cycle_instance'] = False
                result.append(d)
        else:
            if t['repeat_paused']: continue
            rule = t['repeat_rule']
            match = False
            if rule == 'daily': match = True
            elif rule == 'workday': match = wd < 5
            elif rule == 'custom':
                match = wd in (d['custom_days'] or [])
            if match:
                d['is_cycle_instance'] = True
                ck = db.execute("SELECT * FROM checkins WHERE task_id=? AND checkin_date=?", (t['id'],target_date.isoformat())).fetchone()
                if ck:
                    d['status'] = ck['status']
                    d['actual_duration'] = ck['actual_duration']
                    d['review_note'] = ck['review_note']
                    d['completed_at'] = ck['completed_at']
                else:
                    d['status'] = 'incomplete'
                    d['actual_duration'] = None
                    d['review_note'] = None
                    d['completed_at'] = None
                result.append(d)
    prio = {'high':0,'mid':1,'low':2}
    result.sort(key=lambda x: prio.get(x.get('priority','mid'),1))
    return result

@app.route('/api/tasks/today', methods=['GET'])
@token_required
def today_tasks():
    db = get_db()
    tasks = expand_cycle_tasks(g.user_id, db, datetime.date.today())
    done = sum(1 for t in tasks if t['status']=='completed')
    partial = sum(1 for t in tasks if t['status']=='partial')
    return jsonify({'tasks':tasks,'done':done,'partial':partial,'pending':len(tasks)-done-partial,'total':len(tasks)})

@app.route('/api/tasks/tomorrow', methods=['GET'])
@token_required
def tomorrow_tasks():
    db = get_db()
    tasks = expand_cycle_tasks(g.user_id, db, datetime.date.today()+datetime.timedelta(days=1))
    return jsonify({'tasks':tasks,'total':len(tasks)})

@app.route('/api/tasks/defer/<tid>', methods=['POST'])
@token_required
def defer_task(tid):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (tid,g.user_id)).fetchone()
    if not task: return jsonify({'error':'任务不存在'}), 404
    tmr = (datetime.date.today()+datetime.timedelta(days=1)).isoformat()
    db.execute("UPDATE tasks SET target_date=?,status='incomplete',actual_duration=NULL,completed_at=NULL WHERE id=?", (tmr,tid))
    db.commit()
    return jsonify({'msg':'已延期至明天'})

# ============ History & Streak & Summary ============
@app.route('/api/history/calendar', methods=['GET'])
@token_required
def history_calendar():
    year = int(request.args.get('year', datetime.date.today().year))
    month = int(request.args.get('month', datetime.date.today().month))
    db = get_db()
    start = datetime.date(year, month, 1)
    end = datetime.date(year, month, 31) if month==12 else datetime.date(year, month+1, 1)-datetime.timedelta(days=1)
    rows = db.execute("""SELECT checkin_date, status, actual_duration FROM checkins
        WHERE user_id=? AND checkin_date>=? AND checkin_date<=?""",
        (g.user_id, start.isoformat(), end.isoformat())).fetchall()
    cal = {}
    for r in rows:
        d = r['checkin_date']
        if d not in cal:
            cal[d] = {'status':'empty','minutes':0}
        cal[d]['minutes'] += r['actual_duration'] or 0
        if r['status']=='completed': cal[d]['status']='completed'
        elif r['status']=='partial' and cal[d]['status']!='completed': cal[d]['status']='partial'
        elif cal[d]['status']=='empty': cal[d]['status']='empty'
    return jsonify(cal)

@app.route('/api/history/streak', methods=['GET'])
@token_required
def history_streak():
    db = get_db()
    acc = ensure_point_account(g.user_id, db)
    db.commit()
    return jsonify({'streak_days':acc['streak_days'],'max_streak_days':acc['max_streak_days']})

@app.route('/api/history/summary', methods=['GET'])
@token_required
def history_summary():
    week_start_str = request.args.get('week_start')
    db = get_db()
    if week_start_str:
        ws = datetime.date.fromisoformat(week_start_str)
    else:
        today = datetime.date.today()
        ws = today - datetime.timedelta(days=today.weekday())
    we = ws + datetime.timedelta(days=6)
    rows = db.execute("""SELECT c.*, t.name as task_name FROM checkins c
        LEFT JOIN tasks t ON c.task_id=t.id
        WHERE c.user_id=? AND c.checkin_date>=? AND c.checkin_date<=?""",
        (g.user_id, ws.isoformat(), we.isoformat())).fetchall()
    total_minutes = sum(r['actual_duration'] or 0 for r in rows)
    missed = {}
    for r in rows:
        if r['status'] != 'completed' and r['task_name']:
            missed[r['task_name']] = missed.get(r['task_name'],0)+1
    missed_list = sorted(missed.items(), key=lambda x:-x[1])[:3]
    missed_str = '、'.join(f"{n}({c}次)" for n,c in missed_list) if missed_list else '无'
    summary_text = f"本周学习 {total_minutes//60} 小时 {total_minutes%60} 分钟，{missed_str}经常完不成"
    row = db.execute("SELECT * FROM weekly_summaries WHERE user_id=? AND week_start=?", (g.user_id,ws.isoformat())).fetchone()
    if row:
        db.execute("UPDATE weekly_summaries SET total_minutes=?,summary_text=?,missed_tasks=? WHERE id=?",
                   (total_minutes,summary_text,json.dumps(missed,ensure_ascii=False),row['id']))
    else:
        db.execute("INSERT INTO weekly_summaries(id,user_id,week_start,total_minutes,summary_text,missed_tasks) VALUES(?,?,?,?,?,?)",
                   (gen_id(),g.user_id,ws.isoformat(),total_minutes,summary_text,json.dumps(missed,ensure_ascii=False)))
    db.commit()
    return jsonify({'week_start':ws.isoformat(),'total_minutes':total_minutes,
                    'total_hours':round(total_minutes/60,1),'summary_text':summary_text,'missed_tasks':missed})

@app.route('/api/history/day/<date>', methods=['GET'])
@token_required
def history_day(date):
    db = get_db()
    rows = db.execute("""SELECT c.*, t.name as task_name, t.tag, t.priority FROM checkins c
        LEFT JOIN tasks t ON c.task_id=t.id
        WHERE c.user_id=? AND c.checkin_date=?""", (g.user_id, date)).fetchall()
    return jsonify([row_to_dict(r) for r in rows])

# ============ Goals Progress ============
@app.route('/api/goals/progress', methods=['GET'])
@token_required
def goals_progress():
    db = get_db()
    today = datetime.date.today()
    ws = today - datetime.timedelta(days=today.weekday())
    we = ws + datetime.timedelta(days=6)
    goals = db.execute("SELECT * FROM goals WHERE user_id=?", (g.user_id,)).fetchall()
    result = []
    for gl in goals:
        actual = db.execute("""SELECT COUNT(*) as c FROM checkins WHERE user_id=? AND status='completed'
            AND checkin_date>=? AND checkin_date<=? AND task_id IN
            (SELECT id FROM tasks WHERE user_id=? AND tag=?)""",
            (g.user_id, ws.isoformat(), we.isoformat(), g.user_id, gl['tag'])).fetchone()['c']
        result.append({'goal_id':gl['id'],'name':gl['name'],'tag':gl['tag'],
                       'target':gl['target'],'actual':actual,'achieved':actual>=gl['target']})
    return jsonify(result)

# ============ SSE Reminders ============
@app.route('/api/reminders/stream', methods=['GET'])
@token_required
def reminders_stream():
    uid = g.user_id
    q = queue.Queue()
    with SSE_LOCK:
        SSE_CLIENTS.setdefault(uid, []).append(q)
    def gen():
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with SSE_LOCK:
                if uid in SSE_CLIENTS and q in SSE_CLIENTS[uid]:
                    SSE_CLIENTS[uid].remove(q)
    return Response(gen(), mimetype='text/event-stream', headers={
        'Cache-Control':'no-cache','X-Accel-Buffering':'no','Connection':'keep-alive'})

def reminder_scheduler():
    last_remind = {}
    while True:
        try:
            db = sqlite3.connect(DB_PATH)
            db.row_factory = sqlite3.Row
            now = datetime.datetime.now()
            today = now.date().isoformat()
            cur_time = now.strftime('%H:%M')
            tasks = db.execute("""SELECT t.*, s.dnd_start, s.dnd_end FROM tasks t
                LEFT JOIN settings s ON t.user_id=s.user_id
                WHERE t.remind_on=1 AND t.remind_time=? AND t.repeat_paused=0""", (cur_time,)).fetchall()
            for t in tasks:
                if is_in_dnd(t['dnd_start'], t['dnd_end'], now): continue
                ck = db.execute("SELECT * FROM checkins WHERE task_id=? AND checkin_date=? AND status='completed'",
                                (t['id'],today)).fetchone()
                if ck: continue
                key = (t['id'], today)
                if key in last_remind:
                    elapsed = (now - last_remind[key]).total_seconds()/60
                    if elapsed < (t['remind_interval'] or 15): continue
                last_remind[key] = now
                with SSE_LOCK:
                    clients = SSE_CLIENTS.get(t['user_id'], [])
                    msg = f"event: reminder\ndata: {json.dumps({'task_id':t['id'],'task_name':t['name'],'remind_mode':t['remind_mode'],'timestamp':now.isoformat()})}\n\n"
                    for c in clients:
                        try: c.put_nowait(msg)
                        except: pass
            db.close()
        except Exception: pass
        time.sleep(60)

# ============ Export & Health ============
@app.route('/api/export/csv', methods=['GET'])
@token_required
def export_csv():
    typ = request.args.get('type','all')
    db = get_db()
    buf = io.StringIO()
    w = csv.writer(buf)
    if typ in ('points','all'):
        w.writerow(['# 积分明细'])
        w.writerow(['时间','类型','数量','说明','来源任务','来源时长','兑换商品'])
        rows = db.execute("SELECT * FROM points_log WHERE user_id=? ORDER BY time DESC", (g.user_id,)).fetchall()
        for r in rows:
            w.writerow([r['time'],r['type'],r['amount'],r['desc'] or '',r['source_task_id'] or '',r['source_duration'] or '',r['redeem_item_id'] or ''])
        if typ=='all': w.writerow([])
    if typ in ('redeem','all'):
        w.writerow(['# 兑换历史'])
        w.writerow(['时间','商品名','消耗积分','原链接','图片链接'])
        rows = db.execute("SELECT * FROM redeem_history WHERE user_id=? ORDER BY time DESC", (g.user_id,)).fetchall()
        for r in rows:
            w.writerow([r['time'],r['item_name'],r['points'],r['item_link'] or '',r['item_image'] or ''])
    content = buf.getvalue()
    if not content.strip():
        return jsonify({'error':'暂无数据可导出'}), 400
    return Response(content, mimetype='text/csv', headers={'Content-Disposition':'attachment; filename=export.csv'})

@app.route('/api/health', methods=['GET'])
def health():
    try:
        db = get_db()
        db.execute("SELECT 1")
        return jsonify({'status':'ok','time':datetime.datetime.now().isoformat(),'db':'ok'})
    except:
        return jsonify({'status':'error','time':datetime.datetime.now().isoformat(),'db':'error'}), 503

# ============ Admin Calendar ============
@app.route('/api/admin/users/<int:uid>/calendar', methods=['GET'])
@admin_required
def admin_user_calendar(uid):
    year = int(request.args.get('year', datetime.date.today().year))
    month = int(request.args.get('month', datetime.date.today().month))
    db = get_db()
    start = datetime.date(year, month, 1)
    end = datetime.date(year, month, 31) if month==12 else datetime.date(year, month+1, 1)-datetime.timedelta(days=1)
    rows = db.execute("""SELECT checkin_date, status, actual_duration FROM checkins
        WHERE user_id=? AND checkin_date>=? AND checkin_date<=?""",
        (uid, start.isoformat(), end.isoformat())).fetchall()
    cal = {}
    for r in rows:
        d = r['checkin_date']
        if d not in cal: cal[d] = {'status':'empty','minutes':0}
        cal[d]['minutes'] += r['actual_duration'] or 0
        if r['status']=='completed': cal[d]['status']='completed'
        elif r['status']=='partial' and cal[d]['status']!='completed': cal[d]['status']='partial'
    return jsonify(cal)

# ============ Static ============
@app.route('/')
def index():
    return send_from_directory('static','index.html')

init_db()

t = threading.Thread(target=reminder_scheduler, daemon=True)
t.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)),
            debug=os.environ.get('FLASK_ENV') != 'production', threaded=True)