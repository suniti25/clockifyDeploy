## Leave Management System


## 📦 Setup

1. **Clone the repo**:
   ```bash
   git clone https://github.com/Avinto-IT/leave-management-system-backend.git
   cd leave-management-system-backend
   
2. **Create virtual environment**:
   ```bash
   python -m venv .venv
   source venv/bin/activate

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt

4. **Set up environment variables**:
   - Create a .env file in the project root:
   - You can find the sample of .env file in the env.example file in the project root.
     
   ```bash
   # Example
    SECRET_KEY=your-secret-key
    CORS_ALLOWED_ORIGINS=http://localhost:3000,http://127.0.0.1:8000
    DATABASE_URL=postgresql://<db_user>:<db_password>@127.0.0.1:5432/<db_name>

5. **Create and connect to the postgres database based on `DATABASE_URL` used in `.env` file**

6. **Create and apply migrations**:
   ```bash
   python manage.py makemigrations
   python manage.py migrate

7. **Create super user for admin priviledges**:
   ```bash
   python manage.py createsuperuser

8. **Run the server**:
   ```bash
   python manage.py runserver
   
9. Open in browser at: http://127.0.0.1:8000
