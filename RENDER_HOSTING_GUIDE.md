# 🚀 PaidLike Bot — Termux + Render Hosting সম্পূর্ণ গাইড

---

## 📋 প্রক্রিয়া সারসংক্ষেপ

1. **Termux এ রান করে Telegram লগইন করুন** → সেশন ফাইল তৈরি হবে
2. **সেশন ফাইল + কোড GitHub এ আপলোড করুন**
3. **Render.com এ ডিপ্লয় করুন** → আর লগইন লাগবে না!

---

## ধাপ ১: Termux এ সেটআপ ও লগইন

### 1.1 Termux ওপেন করুন, প্যাকেজ ইনস্টল করুন:
```bash
pkg update && pkg upgrade -y
pkg install python git -y
pip install flask telethon requests
```

### 1.2 ফাইল ডাউনলোড করুন:
```bash
cd /storage/emulated/0/
mkdir -p web_like_bot
cd web_like_bot
```

এখানে ৪টি ফাইল রাখুন:
- `app.py`
- `requirements.txt`
- `render.yaml`
- `Procfile`

### 1.3 প্রথমবার রান করুন:
```bash
cd /storage/emulated/0/web_like_bot
python app.py
```

**প্রথমবার Telegram লগইন চাইবে:**
```
Enter phone number or bot token: +880XXXXXXXXX
Enter the code you received: 12345
```
লগইন সফল হলে `data/` ফোল্ডারে সেশন ফাইল তৈরি হবে।

### 1.4 লগইন হয়ে গেলে Ctrl+C দিয়ে বন্ধ করুন

---

## ধাপ ২: GitHub এ সব আপলোড

### 2.1 ফাইল স্ট্রাকচার:
```
paidlike-bot/
├── app.py
├── requirements.txt
├── render.yaml
├── Procfile
└── data/
    └── my_telegram_session.session
```

### 2.2 GitHub Repository তৈরি করুন:
1. [github.com](https://github.com) এ যান
2. **New Repository** → নাম: `paidlike-bot` (Private রাখুন!)
3. Create repository

### 2.3 Git দিয়ে আপলোড (Termux থেকে):
```bash
cd /storage/emulated/0/web_like_bot

git init
git add app.py requirements.txt render.yaml Procfile
git add data/my_telegram_session.session
git commit -m "PaidLike Bot with session"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/paidlike-bot.git
git push -u origin main
```

⚠️ **গুরুত্বপূর্ণ:** সেশন ফাইল `.session` এক্সটেনশনের হয়। নিশ্চিত করুন যে ফাইলটি `data/` ফোল্ডারে আছে।

---

## ধাপ ৩: Render.com এ ডিপ্লয়

### 3.1 Render এ যান:
→ [render.com](https://render.com)

### 3.2 GitHub দিয়ে লগইন করুন

### 3.3 New Web Service তৈরি:
1. **New +** → **Web Service**
2. আপনার `paidlike-bot` repository সিলেক্ট
3. Settings:
   - **Name**: `paidlike-bot`
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python app.py`
   - **Plan**: Free

### 3.4 Environment Variables যোগ করুন:
| Key | Value |
|-----|-------|
| `DATA_DIR` | `/tmp/paidlike_data` |

### 3.5 Create Web Service ক্লিক → অটো ডিপ্লয় শুরু

---

## ধাপ ৪: ডিপ্লয়মেন্ট চেক

Render URL পাবেন, যেমন: `https://paidlike-bot.onrender.com`

### টেস্ট করুন:
- ✅ `https://paidlike-bot.onrender.com/` → ড্যাশবোর্ড লগইন
- পাসওয়ার্ড: `admin123`
- ✅ `https://paidlike-bot.onrender.com/bot` → বট স্ট্যাটাস

---

## ⚠️ জরুরি তথ্য

### Render Free Plan:
- **15 মিনিট নিষ্ক্রিয়তায় sleep** → বট কাজ করবে না
- নতুন রিকোয়েস্টে 30-60 সেকেন্ডে জাগবে
- **মাসে 750 ঘণ্টা ফ্রি**

### SQLite ডেটা:
- সার্ভার restart এ `/tmp` ডেটা মুছে যেতে পারে
- পারমানেন্ট ডেটার জন্য Render Disk ব্যবহার করুন

### সার্ভার সবসময় জাগা রাখতে:
- [UptimeRobot](https://uptimerobot.com) এ সাইন আপ
- HTTP monitor তৈরি করুন → আপনার Render URL দিন
- প্রতি 5 মিনিটে ping → sleep হবে না

### সেশন ফাইল:
- `.session` ফাইল GitHub repo তে থাকলে Render অটো পাবে
- নতুন ফোন নম্বর/ডিভাইস থেকে লগইন করতে হলে Termux এ আবার লগইন করে নতুন session আপলোড

---

## 🔑 ডিফল্ট ক্রেডেনশিয়াল

- **Dashboard Password**: `admin123`
- **Owner Required Channel**: `siam_bhai_official` (সরানো যাবে না)

---

## 📌 ফাইল সারসংক্ষেপ

| ফাইল | কাজ |
|-------|-----|
| `app.py` | প্রধান অ্যাপ্লিকেশন (Flask + Telethon) |
| `requirements.txt` | Python ডিপেন্ডেন্সি |
| `render.yaml` | Render কনফিগারেশন |
| `Procfile` | প্রসেস ডেফিনেশন |
| `data/*.session` | Telegram সেশন ফাইল |
