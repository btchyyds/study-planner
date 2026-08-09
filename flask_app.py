import sys, os
project_path = '/home/' + os.environ.get('PA_USERNAME', 'studyplanner') + '/mysite'
if project_path not in sys.path:
    sys.path.insert(0, project_path)
os.environ.setdefault('DB_PATH', project_path + '/study.db')
from app import app as application