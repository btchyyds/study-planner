# PythonAnywhere 部署说明

## 优势
- 免费层不需要绑定信用卡
- 支持 Flask + SQLite 文件持久化（数据不丢失）
- 公网 URL：`用户名.pythonanywhere.com`

## 部署步骤

### 1. 注册账号
- 打开 https://www.pythonanywhere.com/registration/register/beginner/
- 注册免费账号（不需要信用卡）

### 2. 上传代码
- 登录后点击 **Files** 标签
- 在 `/home/用户名/` 下创建 `mysite` 目录
- 上传以下文件到 `mysite` 目录：
  - `app.py`
  - `flask_app.py`
  - `requirements.txt`
  - `static/` 目录（含 index.html）

或者用 Git 克隆：
- 点击 **Consoles** → **Bash console**
- 执行：`git clone https://github.com/btchyyds/study-planner.git ~/mysite`

### 3. 安装依赖
- 在 Bash console 中执行：
```bash
cd ~/mysite
pip3 install --user flask flask-cors pyjwt werkzeug
```

### 4. 创建 Web App
- 点击 **Web** 标签 → **Add a new web app**
- 选择 **Manual configuration** → 选择 Python 版本（3.10 或更高）
- **Source code** 路径填：`/home/用户名/mysite`
- **Working directory** 路径填：`/home/用户名/mysite`

### 5. 配置 WSGI 文件
- 点击 **Web** 标签 → **WSGI configuration file** 链接
- 替换文件内容为：
```python
import sys, os
project_home = '/home/用户名/mysite'
if project_home not in sys.path:
    sys.path.insert(0, project_home)
os.environ['JWT_SECRET'] = 'this-is-a-very-strong-secret-key-32chars'
os.environ['FLASK_ENV'] = 'production'
os.environ['DB_PATH'] = '/home/用户名/mysite/study.db'
from flask_app import app as application
```
- 把 `用户名` 替换为你的 PythonAnywhere 用户名

### 6. 配置静态文件
- 在 **Web** 标签 → **Static files** 添加：
  - URL: `/` → Directory: `/home/用户名/mysite/static/`

### 7. 重启 Web App
- 点击 **Web** 标签 → **Reload** 按钮

### 8. 访问网站
- 打开 `https://用户名.pythonanywhere.com`
- 任何人都可以注册并使用

## 注意事项
- 免费层不支持后台线程，提醒功能通过前端轮询实现（每 60 秒检查一次）
- 免费层 3 个月不登录会休眠，定期登录即可
- SQLite 数据库文件持久化，重启不丢失
- 管理员账号：admin / admin123