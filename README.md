# FastAPI AI Chatbot (FB Messenger + WhatsApp + Gemini AI)

 lightweight, zero-cost AI chatbot backend written in **FastAPI** with a built-in **Tailwind CSS Admin Panel**.

## 🌟 Features
- 🤖 **Google Gemini 2.0 Integration**: Generates free & smart responses in Bangla and English.
- 📱 **FB Messenger Webhook**: Automatic auto-reply to Facebook page messages.
- 💬 **WhatsApp Cloud API Webhook**: Automatic auto-reply to WhatsApp messages.
- 🧠 **Dynamic Knowledge Base**: Easily add products, pricing, and FAQs via Admin Panel without code changes.
- 📊 **Tailwind CSS Dashboard**: Modern dark-mode admin UI for managing stats & knowledge base.

---

## 🚀 How to Run Locally

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Edit `.env` file with your credentials:
```env
GEMINI_API_KEY=your_gemini_api_key
FB_PAGE_ACCESS_TOKEN=your_fb_token
FB_VERIFY_TOKEN=your_custom_verify_token
WA_ACCESS_TOKEN=your_whatsapp_token
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
```

### 3. Start Development Server
```bash
uvicorn app.main:app --reload
```

- **API Base URL**: `http://localhost:8000`
- **Admin Dashboard**: `http://localhost:8000/admin`
- **Swagger Docs**: `http://localhost:8000/docs`
