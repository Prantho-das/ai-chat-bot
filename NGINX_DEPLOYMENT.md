# NGINX Deployment & Production Guide (FastAPI AI Chatbot)

এই প্রজেক্টটি NGINX সার্ভার, Systemd Service এবং Gunicorn/Uvicorn দিয়ে যেকোনো Ubuntu/Debian VPS (যেমন: DigitalOcean, AWS EC2, Hetzner, Linode, Nginx VPS) এ কীভাবে হোস্ট করবেন তার পূর্ণাঙ্গ নির্দেশিকা।

---

## 🛠️ Step 1: VPS এ প্রজেক্ট ফাইল আপলোড & Dependency ইনস্টল করা

### ১.১ VPS এ লগইন ও আপডেট:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3-pip python3-venv nginx certbot python3-certbot-nginx -y
```

### ১.২ প্রজেক্ট ডিরেক্টরিতে যাওয়া ও Virtual Environment তৈরি:
```bash
cd /var/www
sudo git clone <YOUR_GIT_REPO_URL> marketflow
cd marketflow

# Virtual Environment & Packages
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## ⚙️ Step 2: Systemd Service তৈরি (সার্ভার রিস্টার্ট হলেও বট অন থাকবে)

একটি systemd service ফাইল তৈরি করুন যাতে FastAPI ব্যাকএন্ড ব্যাকগ্রাউন্ডে অনবরত চলতে থাকে।

```bash
sudo nano /etc/systemd/system/marketflow.service
```

নিচের কোডটি পেস্ট করুন:
```ini
[Unit]
Description=MarketFlow FastAPI AI Chatbot Engine
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/marketflow
ExecStart=/var/www/marketflow/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

সার্ভিস চালু ও এনেবল করুন:
```bash
sudo systemctl daemon-reload
sudo systemctl start marketflow
sudo systemctl enable marketflow
```

---

## 🌐 Step 3: NGINX Configuration (Reverse Proxy Setup)

NGINX কনফিগারেশন ফাইল তৈরি করুন:

```bash
sudo nano /etc/nginx/sites-available/marketflow
```

নিচের NGINX রিভার্স প্রক্সি কনফিগারেশন পেস্ট করুন (`your-domain.com` এর জায়গায় আপনার ডোমেইন নেম দিন):

```nginx
server {
    listen 80;
    server_name your-domain.com www.your-domain.com;

    # Client max body size for file uploads
    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;

        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Static Assets Caching
    location /static/ {
        alias /var/www/marketflow/app/static/;
        expires 30d;
    }

    error_log /var/log/nginx/marketflow_error.log;
    access_log /var/log/nginx/marketflow_access.log;
}
```

কনফিগারেশন এনাবল করুন ও NGINX টেস্ট করুন:
```bash
sudo ln -s /etc/nginx/sites-available/marketflow /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

---

## 🔒 Step 4: SSL (HTTPS) ফ্রি সার্টিফিকেট ইনস্টল (Certbot / Let's Encrypt)

Facebook Messenger ও WhatsApp Webhooks অবশ্যই **`https://`** চায়। ফ্রি SSL ইনস্টল করতে:

```bash
sudo certbot --nginx -d your-domain.com -d www.your-domain.com
```

Certbot স্বয়ংক্রিয়ভাবে NGINX কনফিগারেশন আপডেট করে দিবে এবং HTTPS সক্রিয় করবে!

---

## 🎉 Facebook & WhatsApp Webhook URLs (প্রোডাকশনে ব্যবহার করবেন)

- **Facebook Webhook Callback URL**: `https://your-domain.com/webhook/messenger`
- **WhatsApp Webhook Callback URL**: `https://your-domain.com/webhook/whatsapp`
- **Admin Dashboard**: `https://your-domain.com/admin`
