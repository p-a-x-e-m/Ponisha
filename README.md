<a id="fa"></a>
# ponishabot

ربات خودکار برای [ponisha.ir](https://ponisha.ir) (بازار آزاد فریلنسری ایران):
پروژه‌های تازه را پایش می‌کند، با پروفایل مهارت‌های شما تطبیق می‌دهد، اعلان تلگرام
می‌فرستد و می‌تواند از طریق کروم (سلنیوم) به‌صورت خودکار پیشنهاد ارسال کند. یک موتور
تصمیم‌گیری هوش مصنوعی قابل‌تعویض (هر endpoint سازگار با OpenAI، مثلاً 9router)
تصمیم می‌گیرد که آیا پیشنهاد بدهیم، با چه قیمت و چه مدتی.

رابط وب یک **داشبورد محلی جنگو** است با ظاهری الهام‌گرفته از پنل 9Router.

**زبان:** فارسی (این بخش) · [English](#en)

**محتوا:** [معماری](#fa-arch) · [نصب](#fa-setup) · [اجرا](#fa-run) · [ورود](#fa-login) ·
[پیکربندی](#fa-config) · [داشبورد](#fa-dashboard) · [قیمت‌گذاری](#fa-pricing) ·
[امنیت](#fa-security) · [استقرار](#fa-deploy) · [عیب‌یابی](#fa-troubleshooting) ·
[ساختار پوشه‌ها](#fa-layout)

> ⚠️ **سه قاعدهٔ طلایی**
>
> 1. **همیشه یک پروسه.** ربات یک singleton دارد (رشتهٔ مانیتور + درایور سلنیوم).
>    چند worker یا چند نمونه همزمان یعنی **پیشنهاد تکراری**.
> 2. **هرگز autoreload.** ریلود وسط کار، رشتهٔ مانیتور و کروم را می‌کُشد.
> 3. **داشبورد احراز هویت ندارد.** فقط روی `127.0.0.1` نگهش دارید — جزئیات در
>    [امنیت](#fa-security).

---

<a id="fa-arch"></a>
## معماری

یک چرخهٔ پایش این شکلی است:

```
scraper          لیستینگ و صفحهٔ جزئیات را می‌خواند (HTML + JSON پنهانِ __NEXT_DATA__)
   │
   ▼
monitor          می‌دود: فیلتر مهارت → صف‌بندی اولویت (پروژه‌های تازه اول) → dedup
   │
   ▼
decision         آیا بید بزنیم؟ با چه قیمت/مدت؟
   ├─ LLMBidDecision      مدل + دادهٔ بازار Karlancer + نرخ فارکس
   └─ HeuristicBidDecision   قواعد قیمت‌گذاری رقابتی (بدون شبکه)
   │
   ├──────────────► notifier   اعلان تلگرام
   │
   ▼
bidder           فرم پیشنهاد را با سلنیوم پر و ارسال می‌کند (فقط وقتی dry_run=false)
```

لایهٔ `botstate` همهٔ این‌ها را به‌صورت singleton نگه می‌دارد، رویدادها را در صف
می‌ریزد و هر ۲ ثانیه یک `status_snapshot` برای داشبورد می‌سازد؛ `app.py` سیم‌کشی
قطعات را به عهده دارد.

### ماژول‌ها

| فایل | خط | نقش |
|---|---:|---|
| [ponishabot/app.py](ponishabot/app.py) | 575 | سیم‌کشی: session، scraper، monitor، bidder، notifier |
| [ponishabot/auth.py](ponishabot/auth.py) | 625 | ورود OTP و لاگین مرورگر، کوکی `pss-at`، درایور کروم |
| [ponishabot/bidder.py](ponishabot/bidder.py) | 480 | پر کردن و ارسال فرم پیشنهاد، تشخیص موفقیت |
| [ponishabot/botstate.py](ponishabot/botstate.py) | 326 | singleton، صف رویداد، وضعیت، سهمیه، نرخ دلار |
| [ponishabot/config.py](ponishabot/config.py) | 186 | خواندن/نوشتن `config.yaml` |
| [ponishabot/decision.py](ponishabot/decision.py) | 882 | موتور تصمیم: LLM و هیوریستیک |
| [ponishabot/forex.py](ponishabot/forex.py) | 89 | نرخ USDT/Toman از Wallex + فاکتور افزایش قیمت |
| [ponishabot/karlancer_scraper.py](ponishabot/karlancer_scraper.py) | 254 | مقایسهٔ قیمت بازار از karlancer.com (فقط-خواندنی) |
| [ponishabot/models.py](ponishabot/models.py) | 64 | `Project` / `Profile` / `BidPlan` |
| [ponishabot/monitor.py](ponishabot/monitor.py) | 269 | حلقهٔ پایش، فیلتر، gate سهمیه |
| [ponishabot/notifier.py](ponishabot/notifier.py) | 94 | ارسال اعلان تلگرام |
| [ponishabot/tui/](ponishabot/tui/) | 700 | رابط کاربری متنی: صفحه‌ها، کلیدها، فیلتر لاگ |
| [ponishabot/paths.py](ponishabot/paths.py) | 23 | مسیر همهٔ فایل‌ها (نسبت به ریشه، نه CWD) |
| [ponishabot/scraper.py](ponishabot/scraper.py) | 550 | اسکرپ لیستینگ/جزئیات، خواندن سهمیه |
| [ponishabot/skills.py](ponishabot/skills.py) | 120 | کاتالوگ مهارت‌های پونیشا (کش ۷ روزه) |
| [dashboard/](dashboard/) | 611 | ویوها و URLهای رابط وب |
| [web/](web/) | 99 | تنظیمات پروژهٔ جنگو |

---

<a id="fa-setup"></a>
## نصب

نیازمندی‌ها: **Python 3.10+** (جنگو ۵)، **گوگل کروم** نصب‌شده روی همان دستگاه.

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

در لینوکس به‌جای `venv\Scripts\activate` باید `source venv/bin/activate` بزنید.

> **ناسازگاری‌ای که باید بدانید:** [requirements.txt](requirements.txt) می‌گوید
> `django>=5.0,<6`، ولی محیطی که همین الان روی این دستگاه نصب است **Django 6.1.1 روی
> Python 3.14.7** دارد. یعنی `pip install -r requirements.txt` در نصب تازه، جنگو را از
> ۶٫۱٫۱ به **۵٫۲٫۱۷** پایین می‌آورد و سازگاری بین‌نسخه‌ای‌اش با بقیهٔ وابستگی‌ها را
> هم عوض می‌کند. اگر می‌خواهید همان جنگوی نصب‌شده را نگه دارید، آن دستور را کورکورانه
> اجرا نکنید.
>
> گواه `django>=5.0,<6` با Django 6.x هم ناسازگار است؛ `pip install -r requirements.txt`
> پایین‌آوردن را ترجیح می‌دهد، نه رد کردن نصب.

### تست‌ها

```bash
python -m unittest discover -s tests -v
```

[`tests/`](tests/) بدون هیچ وابستگی اضافه‌ای اجرا می‌شود (فقط `unittest` استاندارد) و
**آفلاین** است: هیچ درخواست شبکه‌ای، هیچ سلنیومی، و هیچ نشست پونیشا لازم ندارد. روی
توابع خالص تمرکز دارد — همان‌جایی که اشتباه‌های گران اتفاق می‌افتد:

| گروه | چه چیزی را قفل می‌کند |
|---|---|
| `ScraperText` | نرمال‌سازی ارقام فارسی، پارس بودجه، و **حذف کاروسل** — تا بودجهٔ یک پروژهٔ همسایه دوباره جای بودجهٔ کارفرما ننشیند |
| `ForexUplift` | ضریب دلار؛ مخصوصاً اینکه با ریال قوی‌تر قیمت **زیر** برآورد مدل نمی‌رود |
| `HeuristicPricing` | مثال عددی README (۹٬۰۰۰٬۰۰۰ / ۱۵ روز)، کف لیستینگ، و رد نشدن پروژهٔ بدون تگ مهارت |
| `AIPricingAdjustments` | کلَمپ Karlancer در هر دو حالت فعال/غیرفعال، گرد‌کردن، و بالا کشیدن به کف |
| `LLMResponseParsing` | JSON خراب یا محصور در fence نباید exception بدهد (باید به هیوریستیک برگردد) |
| `PanelLogHandling` | جمع‌کردن خطوط تکراری (`Forex fetch failed ×۹`)، فیلتر نویز، و اینکه رویدادهای ربات در دید پیش‌فرض بمانند |
| `PanelFormatting` | قالب پول/زمان و امنیت markup — پیام لاگ با `[` نباید rich را کرش کند |
| `TerminalSingleton` | رگرسیون: `main.py` نباید `BotApp` دوم بسازد (وگرنه پنل روی نمونهٔ اشتباه گزارش می‌دهد) |
| `ConfigDefaults` | پیش‌فرض‌هایی که README مستند کرده — از جمله `dry_run: true` |

CI روی هر push و PR اجرا می‌شود ([.github/workflows/tests.yml](.github/workflows/tests.yml))
روی Python های ۳٫۱۰ تا ۳٫۱۳.

> **چرا تست‌ها مهم‌اند:** تاریخچهٔ این پروژه چند باگ دارد که همه **موفقیت گزارش دادند
> در حالی که هیچ کاری نکرده بودند** — بودجهٔ اشتباه خوانده شد، پیشنهاد زیر کف فرستاده
> شد، پروژه‌ای که هرگز بید نخورده «انجام‌شده» ثبت شد. این تست‌ها دقیقاً همان مسیرها را
> قفل می‌کنند تا بازنگردند.

---

<a id="fa-run"></a>
## اجرا

```bash
python manage.py bot                # رابط وب → http://127.0.0.1:8000/
python main.py                      # رابط کاربری متنی تعاملی
python main.py --plain              # فقط لاگ، بدون TUI
python manage.py bot_login          # باز کردن کروم برای لاگین دستی
```

### `main.py` — رابط کاربری متنی (TUI)

همان کاری را می‌کند که داشبورد می‌کند، بدون باز کردن مرورگر: وضعیت زنده،
پروژه‌ها، **همهٔ تنظیمات**، و **ورود** — همه داخل ترمینال.

با یک **منو** شروع می‌شود و **مانیتور خودکار شروع نمی‌شود**؛ خودت انتخاب می‌کنی.

```
 ┌──────────────────────────────────────────────┐
 │  ponishabot                                  │
 │  ponisha.ir monitor                          │
 │                                              │
 │  ▸ [1]  Dashboard   live status and controls │
 │    [2]  Projects    scanned listings         │
 │    [3]  Settings    bot configuration        │
 │    [4]  Login       sign in to ponisha       │
 │    [5]  Log         live output              │
 │                                              │
 │    [q]  quit                                 │
 │                                              │
 │  arrows move · Enter opens · or the number   │
 └──────────────────────────────────────────────┘
```

> **رابط انگلیسی است.** داشبورد وب فارسی است چون کسی آن را در مرورگر
> می‌خواند؛ این را در grid ترمینال می‌خوانی، جایی که فارسی واقعاً شکننده
> است — حروف به هم می‌چسبند و عرض رشته با طولش یکی نیست، پس ستون‌ها از
> تراز درمی‌آیند. داده‌ای که از پونی‌شا می‌آید (بج‌هایی مثل «فوری»، پیام‌های
> خطای خود سایت) فارسی می‌ماند چون باید با سایت تطبیق کند.

#### صفحه‌ها

`Tab` و `Shift+Tab` بین این شش صفحه می‌چرخند:

| صفحه | چه‌کار می‌کند |
|---|---|
| **Menu** | انتخاب صفحه با `1`..`5` یا `↑↓` + `Enter` |
| **Dashboard** | وضعیت زنده، آخرین پیشنهادها، شروع/توقف |
| **Projects** | جدول پروژه‌های اسکن‌شده؛ `Enter` جزئیات تصمیم، `/` جست‌وجو، `b` ارسال پیشنهاد |
| **Settings** | همهٔ تنظیمات؛ `Tab` بین فیلدها، `Ctrl+S` ذخیره، `Esc` لغو |
| **Login** | شمارهٔ موبایل → کد پیامک، یا `o` برای ورود با مرورگر |
| **Log** | لاگ زنده؛ `f` سطح، `n` نویز، `c` پاک‌کردن |

#### کلیدها

| کلید | کار |
|---|---|
| `1`..`5` | (در منو) رفتن مستقیم به صفحه |
| `Tab` / `Shift+Tab` | صفحهٔ بعد / قبل |
| `↑` `↓` یا `k` `j` | حرکت در فهرست‌ها |
| `Enter` | تأیید / جزئیات / ویرایش |
| `Esc` | برگشت به منو / لغو ویرایش |
| `m` | برگشت به منو از هر جا |
| `q` | خروج (درایور کروم بسته می‌شود) |

کلیدهای داخل هر صفحه:

| صفحه | کلید | کار |
|---|---|---|
| داشبورد | `s` | شروع / توقف مانیتور |
| داشبورد | `d` | روشن/خاموش کردن `dry_run` |
| پروژه‌ها | `Enter` | جزئیات تصمیم (قیمت، رقبا، دلیل) |
| پروژه‌ها | `/` | رفتن به فیلد جست‌وجو |
| پروژه‌ها | `b` | ارسال دستی پیشنهاد (با `dry_run` غیرفعال) |
| تنظیمات | `Ctrl+S` | ذخیره در `config.yaml` |
| تنظیمات | `Esc` | لغو تغییرات ذخیره‌نشده |
| ورود | `Enter` | ارسال کد، سپس تأیید کد |
| ورود | `o` | ورود با مرورگر (کروم) |
| لاگ | `f` | چرخش سطح (important → debug → info) |
| لاگ | `n` | نمایش/پنهان‌کردن نویز |
| لاگ | `c` | پاک‌کردن |

> **چرا کلید شماره‌ای برای صفحه‌ها نیست:** یک عدد که در سطح برنامه bind شود،
> وقتی فیلد متنی فوکوس دارد هرگز به برنامه نمی‌رسد. یعنی روی داشبورد کار می‌کرد
> و روی صفحهٔ ورود — همان‌جایی که تایپ عدد کل کار است — بی‌صدا می‌شکست. `Tab`
> همیشه می‌رسد، پس تنها راه بین صفحه‌ها همان است.

#### آرگومان‌ها

| آرگومان | پیش‌فرض | توضیح |
|---|---|---|
| `--plain` | خاموش | به‌جای TUI، فقط لاگ را بریزد |
| `--log-level` | `important` | سطح اولیهٔ لاگ |

> **لاگ هوشمند فیلتر می‌شود.** یک اجرای سالم پر از `Forex fetch failed` است که
> هزار بار تکرار می‌شود و بقیهٔ لاگ را زیر خودش می‌برد. TUI خطوط تکراری را جمع
> می‌کند (`×۹`) و نویز شناخته‌شده را پنهان می‌کند — در یک نمونه، **۴۸۱۷ خط خام به
> ۳۰۳ ردیف خواندنی** رسید. کلید `n` همه‌چیز را نشان می‌دهد.

> **تنظیمات ذخیره می‌شود.** `Ctrl+S` در صفحهٔ تنظیمات، همان اعتبارسنجی داشبورد را
> اجرا می‌کند (مقدار خراب رد می‌شود، مقدار خارج از بازه کلیپ می‌شود) و بعد در
> `config.yaml` می‌نویسد — پس تنظیمات آخرین اسکن به یاد می‌ماند.

> **تخریب نرم:** اگر `textual` نصب نباشد یا خروجی به فایل/پایپ برود، خودش به
> حالت لاگ‌محور برمی‌گردد و پیام می‌دهد — نه کرش.

### `manage.py bot`

| آرگومان | env | پیش‌فرض | توضیح |
|---|---|---|---|
| `--host` | `BOT_HOST` | `127.0.0.1` | آدرس bind؛ `0.0.0.0` فقط با محافظ |
| `--port` | `BOT_PORT` | `8000` | پورت |

این دستور خودش `--noreload` و `insecure=True` را ست می‌کند:

- **`--noreload`** واجب است — ریلود جنگو، رشتهٔ مانیتور و پنجرهٔ کروم را وسط کار از بین می‌برد.
- **`insecure=True`** باعث می‌شود `/static/` حتی وقتی `DJANGO_DEBUG=0` سرو شود. بدون آن،
  `app.css` و `poll.js` خطای 404 می‌گیرند و **هیچ دکمه‌ای کار نمی‌کند** — صفحه باز می‌شود
  ولی بی‌واکنش است.

اگر `--host` چیزی غیر از `127.0.0.1`/`localhost` باشد، دستور هشدار احراز هویت چاپ می‌کند.

---

<a id="fa-login"></a>
## ورود به حساب پونیشا

دو مسیر داریم:

| | **OTP** (پیشنهادی) | **لاگین مرورگر** |
|---|---|---|
| از کجا | دکمهٔ ورود در داشبورد | دکمهٔ ورود یا `manage.py bot_login` |
| نیاز به کروم/نمایشگر | **نه** — فقط `requests` | **بله** |
| چه چیزی ذخیره می‌شود | یک Bearer JWT در جدول `meta` | کوکی `pss-at` |
| مناسب | VPS بدون نمایشگر، هدلس | دستگاه محلی با کروم |

جریان OTP دو مرحله است: شمارهٔ موبایل → کد پیامک → تأیید. این مسیر هیچ سلنیومی
استفاده نمی‌کند و بعداً کوکی سایت را از همان توکن می‌سازد، پس ربات می‌تواند بدون مرورگر
کار کند.

`PonishaAuth.auth()` به این ترتیب تصمیم می‌گیرد:

1. کوکی ذخیره‌شده را با **REST API** (نه صفحهٔ HTML) بررسی می‌کند — 401 یعنی نشست مرده.
2. اگر کوکی نبود یا مرده بود، توکن OTP ذخیره‌شده را به کوکی `pss-at` تبدیل می‌کند.
3. فقط در نهایت سراغ کروم می‌رود.

> **تشخیص «لاگین شد یا نه»:** در داشبورد نوار وضعیت `has_cookies` را چک کنید؛ این فیلد
> هم کوکی مرورگر را می‌پذیرد هم توکن OTP را. برای `pss-at` به `PONISHA_TOKEN_PASSPHRASE`
> نیاز است (اگر تعریف نشود، رمز پیش‌فرض داخل کد استفاده می‌شود).

---

<a id="fa-config"></a>
## پیکربندی

دو لایه دارد: **متغیرهای محیطی** (برای اسرار و تنظیمات Django) و **`config.yaml`**
(برای بقیه).

<a id="fa-env"></a>
### متغیرهای محیطی

| متغیر | از کجا خوانده می‌شود | پیش‌فرض | نقش |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `config.py` | — | بر `telegram.bot_token` **مقدم** است |
| `TELEGRAM_CHAT_ID` | `config.py` | — | بر `telegram.chat_id` **مقدم** است |
| `DJANGO_SECRET_KEY` | `web/settings.py` | placeholder فقط‌محلی | کلید جنگو |
| `DJANGO_DEBUG` | `web/settings.py` | `1` | `0` در محیط تولید |
| `DJANGO_ALLOWED_HOSTS` | `web/settings.py` | `127.0.0.1,localhost` | جداشده با کاما |
| `BOT_HOST` | `manage.py bot` | `127.0.0.1` | آدرس bind |
| `BOT_PORT` | `manage.py bot` | `8000` | پورت |
| `PONISHA_TOKEN_PASSPHRASE` | `auth.py` | رمز پیش‌فرض داخل کد | رمزگشایی کوکی `pss-at` |

> ⚠️ **فایل `.env` بارگذاری نمی‌شود.** هیچ `python-dotenv`ای در وابستگی‌ها نیست و هیچ
> پارسر دستی‌ای هم وجود ندارد. ساختن یک فایل `.env` در ریشهٔ پروژه **هیچ اثری ندارد**.
>
> - **محلی (ویندوز/لینوکس):** متغیر را مستقیم در شل ست کنید.
>   `set TELEGRAM_BOT_TOKEN=...` در cmd یا `$env:TELEGRAM_BOT_TOKEN="..."` در PowerShell،
>   و `export TELEGRAM_BOT_TOKEN=...` در bash.
> - **روی VPS با systemd:** از `EnvironmentFile=` استفاده می‌شود. قالب:
>   [deploy/bot.env.example](deploy/bot.env.example) — کپی به `deploy/bot.env` و
>   `chmod 600`.

<a id="fa-config-table"></a>
### رفرنس کامل `config.yaml`

قالب شروع: [config.example.yaml](config.example.yaml) — آن را کپی کنید:

```bash
cp config.example.yaml config.yaml
```

ستون **پیش‌فرض** مقداری است که کد بدون هیچ فایلی استفاده می‌کند؛ ستون **نمونه** مقداری
است که در `config.example.yaml` نوشته شده. این دو بعضی جاها **با هم فرق دارند** و
ستون‌های جدا دارند تا گمراه نشوید.

**`telegram`**

| کلید | پیش‌فرض | نمونه | توضیح |
|---|---|---|---|
| `telegram.bot_token` | `""` | `REPLACE_WITH_BOTTOKEN_FROM_BOTFATHER` | توکن ربات تلگرام |
| `telegram.chat_id` | `""` | `REPLACE_WITH_YOUR_CHAT_ID` | شناسهٔ چت |

**`search`**

| کلید | پیش‌فرض | نمونه | توضیح |
|---|---|---|---|
| `search.skills` | `[]` | `Python`, `Django`, `پایتون (Python)`, `جنگو (Django)` | مهارت‌های شما — باید **عین** رشته‌های پونیشا باشند (`طراحی لوگو`، `جنگو (Django)`). همین فهرست از Settings هم قابل ویرایش است. |

**`monitor`**

| کلید | پیش‌فرض | نمونه | توضیح |
|---|---:|---:|---|
| `monitor.refresh_interval` | **300** | **60** | ثانیه بین هر پایش. در داشبورد بین ۱۰ تا ۳۶۰۰ کلیپ می‌شود. |
| `monitor.max_pages` | **24** | **3** | حداکثر صفحهٔ لیستینگ در هر چرخه |
| `monitor.priority_window_minutes` | 5 | 5 | پروژه‌هایی که ≤ این دقیقه پیش ثبت شده‌اند **اول** بررسی می‌شوند |

**`bid`**

| کلید | پیش‌فرض | نمونه | توضیح |
|---|---:|---:|---|
| `bid.budget_min` | 0 | 0 | کف قیمت شما. در مسیر هیوریستیک به `profile.min_budget` می‌رود و کف قیمتِ نهایی را می‌سازد: `max(این مقدار, ۲٬۰۰۰٬۰۰۰)`. مثلاً `2000000` یعنی هیچ پیشنهادی زیر ۲ میلیون نمی‌رود. **فیلتر پروژه نیست** — پروژهٔ کم‌بودجه رد نمی‌شود، فقط قیمت پایین نمی‌آید. |
| `bid.budget_max` | **10000000** | **25000000** | سقف قیمت شما. در هیوریستیک به `profile.max_budget` می‌رود و **میانهٔ بازهٔ بودجه را قبل از هر تعدیل رقابتی** به این عدد کلیپ می‌کند. فقط در هیوریستیک اثر دارد؛ مسیر AI به سقف کارفرما یا شما پایبند نیست. |
| `bid.proposal_template` | `descriptions.txt` | `descriptions.txt` | فایل متن پیشنهاد (برای مسیرهای هیوریستیک) |
| `bid.min_skill_match` | 0.5 | 0.5 | حداقل همپوشانی مهارت برای موتور هیوریستیک |
| `bid.dry_run` | `true` | `true` | `true` = فقط اعلان، هیچ پیشنهادی فرستاده نمی‌شود |
| `bid.min_remaining_proposals` | 2 | 2 | ربات قبل از رسیدن سهمیه به این عدد متوقف می‌شود |

**`bid.competitive_pricing`** — فقط موتور هیوریستیک

| کلید | پیش‌فرض | نمونه | توضیح |
|---|---:|---:|---|
| `high_competition_threshold` | 15 | 15 | آستانهٔ «رقابت زیاد». اگر تعداد پیشنهادها **≥ این مقدار** باشد تخفیف اعمال می‌شود (یعنی ۱۵ پیشنهاد یا بیشتر). بین ۵ تا ۱۴ پیشنهاد هیچ‌کدام از این پنج کلید جاری نمی‌شود. |
| `high_competition_discount_percent` | 15 | 15 | تخفیف همان حالت: `قیمت × (۱ − ۱۵٪)`. روی قیمت ۷٬۰۰۰٬۰۰۰ → **۵٬۹۵۰٬۰۰۰**. |
| `low_competition_discount_percent` | 10 | 10 | اگر تعداد پیشنهادها **< ۵** باشد → **افزایش** قیمت (نامش «تخفیف» است ولی در عمل حبابِ رقابت کم است). `قیمت × (۱ + ۱۰٪)`: ۷٬۰۰۰٬۰۰۰ → **۷٬۷۰۰٬۰۰۰**. |
| `urgency_premium_percent` | 20 | 20 | اگر پروژه بج «فوری» داشته باشد → `قیمت × (۱ + ۲۰٪)`. ۷٬۰۰۰٬۰۰۰ → **۸٬۴۰۰٬۰۰۰**. **بعد از** تخفیف/حبابِ رقابت اجرا می‌شود، پس روی همان عدد بیشتری می‌نشیند که رقابت ساخته. |
| `premium_badge_multiplier` | 1.15 | 1.15 | اگر بج «متمایز»، «برجسته» یا «حرفه‌ای» داشته باشد → `قیمت × ۱٫۱۵`. ۷٬۰۰۰٬۰۰۰ → **۸٬۰۴۹٬۹۹۹** (یک تومان کمتر از ۸٬۰۵۰٬۰۰۰، چون قیمت با `int()` پایین برده می‌شود). |

**`browser`**

| کلید | پیش‌فرض | نمونه | توضیح |
|---|---|---|---|
| `browser.headless` | `false` | `false` | `true` روی VPS بدون نمایشگر |
| `browser.show_browser` | `false` | **`true`** | پنجرهٔ کروم زنده کنار مانیتور باز شود |
| `browser.user_data_dir` | `null` | `null` | مسیر پروفایل کروم (برای نگه‌داشتن نشست بین اجراها) |

**`ai`**

| کلید | پیش‌فرض | نمونه | توضیح |
|---|---|---|---|
| `ai.enabled` | `false` | `false` | `false` = موتور هیوریستیک |
| `ai.base_url` | `""` | `https://your-router.example/v1` | endpoint سازگار با OpenAI |
| `ai.api_key` | `""` | `REPLACE_WITH_YOUR_AI_API_KEY` | کلید API |
| `ai.model` | `""` | `your-model-name` | نام مدل |
| `ai.min_skill_gate` | 0.2 | 0.2 | حداقل همپوشانی مهارت قبل از فراخوانی مدل |
| `ai.max_description_chars` | 4000 | 4000 | حداکثر کاراکتر توضیحات ارسالی به مدل |
| `ai.temperature` | 0.2 | 0.2 | دمای مدل |
| `ai.karlancer_enabled` | **`false`** | **`true`** | جمع‌آوری دادهٔ بازار Karlancer قبل از تصمیم |
| `ai.karlancer_max_pages` | 2 | 2 | حداکثر صفحهٔ Karlancer |

**`forex`** — افزایش قیمت بر اساس نرخ دلار

| کلید | پیش‌فرض | نمونه | توضیح |
|---|---|---|---|
| `forex.enabled` | `true` | `true` | `false` = هیچ افزایشی با دلار اعمال نمی‌شود و قیمتی که مدل داده همان می‌ماند (کلَمپ Karlancer همچنان جاری است). فقط مسیر AI را تحت تأثیر دارد. |
| `forex.baseline_usd_rate` | **500000** | **229000** | نقطهٔ صفر محاسبه. با نمونه: نرخ فعلی ۲۳۵٬۹۰۲ و مبنا ۲۲۹٬۰۰۰ → `(۲۳۵٬۹۰۲ − ۲۲۹٬۰۰۰) / ۲۲۹٬۰۰۰ = +۳٫۰۱٪`. این را نسبت به **نرخی که خودت ثبت کرده‌ای** می‌سنجی، نه صفر. |
| `forex.sensitivity` | 0.5 | 0.5 | چقدر از آن ۳٫۰۱٪ به قیمت منتقل شود. فرمول: `۱ + (۳٫۰۱٪ × sensitivity)`. با ۰٫۵ → `۱٫۰۱۵۱` و قیمت `۸٬۰۰۰٬۰۰۰ → ۸٬۱۲۰٬۷۹۹`. با `1.0` → `۸٬۲۴۰٬۸۰۰`. با `0` → بدون تغییر. نتیجه هرگز زیر `۱٫۰` نمی‌رود، پس با ارزان‌شدن دلار زیر قیمتِ پیشنهادیِ مدل پایین نمی‌رویم. |
| `forex.source` | `wallex` | `wallex` | تنها منبع پیاده‌سازی‌شده. مقدار دیگری بگذاری نرخی برنمی‌گردد، افزایش قیمت رد می‌شود و ربات به کارش ادامه می‌دهد (fail-open). |

**`karlancer`** — کلَمپ نرم قیمت

| کلید | پیش‌فرض | نمونه | توضیح |
|---|---:|---:|---|
| `karlancer.soft_clamp_enabled` | `true` | `true` | `false` = هیچ سقفی در کار نیست و قیمت AI هرقدر باشد می‌ماند. برای اینکه اصلاً اثری داشته باشد باید `ai.karlancer_enabled` هم `true` باشد و دادهٔ بازار جمع شده باشد. |
| `karlancer.clamp_multiplier` | 3.0 | 3.0 | سقف = میانگین بازار × ۳٫۰. با میانگین ۴٬۰۰۰٬۰۰۰ سقف **۱۲٬۰۰۰٬۰۰۰** است و قیمت ۸٬۰۰۰٬۰۰۰ زیرش می‌ماند (دست نمی‌خورد). با میانگین ۲٬۶۰۰٬۰۰۰ سقف **۷٬۸۰۰٬۰۰۰** است و قیمت ۸٬۰۰۰٬۰۰۰ از آن بالاتر می‌رود → ردیف بعدی اعمال می‌شود. |
| `karlancer.target_multiplier` | 2.5 | 2.5 | وقتی سقف رد شد، قیمت **به میانگین × ۲٫۵ می‌رود** (نه به خود سقف). با میانگین ۲٬۶۰۰٬۰۰۰: `۲٬۶۰۰٬۰۰۰ × ۲٫۵ = ۶٬۵۰۰٬۰۰۰`، یعنی `۸٬۰۰۰٬۰۰۰ → ۶٬۵۰۰٬۰۰۰`. در مثال بالا چون ۶٬۵۰۰٬۰۰۰ بالاتر از کف لیستینگ (۵٬۰۰۰٬۰۰۰) است، همان می‌ماند. |

**`profile`**

| کلید | پیش‌فرض | نمونه | توضیح |
|---|---|---|---|
| `profile.name` | `""` | `""` | نام نمایشی شما |
| `profile.about` | `""` | `Describe your experience, rates, and bid rules for the AI.` | **مهم‌ترین فیلد برای AI**: دستورالعمل‌های شخصی، تجربه و نرخ‌ها. قواعد اینجا **الزام‌آور** تلقی می‌شوند. برای هیوریستیک هم از بازه‌های `X-YM` داخل همین متن برای برآورد قیمت استفاده می‌شود. |
| `profile.hourly_min` | 0 | 0 | حداقل نرخ ساعتی (تومان) |
| `profile.hourly_max` | 0 | 0 | حداکثر نرخ ساعتی (تومان) |

> **ذخیره از داشبورد:** `POST /api/settings` کل `config.yaml` را بازنویسی می‌کند. چون
> `Config` پیش‌فرض‌های خودش را روی کلیدهای غایب می‌گذارد، فایل همیشه کامل بازنویسی
> می‌شود — یعنی کامنت‌های دستی شما داخل `config.yaml` با ذخیره از داشبورد **پاک می‌شوند**.

---

<a id="fa-dashboard"></a>
## داشبورد

`http://127.0.0.1:8000/`

### صفحهٔ اصلی

- **نوار وضعیت:** پیشنهاد باقی‌مانده، متچ‌ها، بیدها، Uptime، نرخ USD/Toman
- **کنترل‌ها:** Start / Stop / ورود، دمو، و کلید `dry_run`
- **کارت پروژه‌ها:** فهرست + جست‌وجو + کارت جزئیات (بودجه، رقبا، بج‌ها، مهارت‌ها،
  توضیحات) با دکمهٔ «ارسال پیشنهاد» دستی و «کپی لینک»
- **لاگ زنده:** با فیلتر و شمارنده، از فایل `ponishabot.log` می‌خواند

### صفحهٔ Settings

| بخش | شامل |
|---|---|
| کنترل‌ها | `headless`، `show_browser`، `dry_run`، فایل پیشنهاد، مسیر پروفایل کروم |
| تلگرام | توکن، شناسهٔ چت + دکمهٔ **تست تلگرام** |
| جستجو و بودجه | مهارت‌ها (با **جست‌وجوگر مهارت‌ها** از کاتالوگ پونیشا)، بازهٔ بودجه، بازهٔ بررسی، پنجرهٔ اولویت، حداقل تطابق، حداکثر صفحات، توقف در سهمیه |
| پروفایل | نام، دستورالعمل‌های AI (`about`)، نرخ ساعتی |
| موتور AI | فعال‌سازی، `karlancer_enabled`، endpoint، کلید، مدل، آستانهٔ مهارت، دما، حداکثر کاراکتر، صفحات Karlancer + دکمهٔ **تست AI** |
| قیمت‌گذاری رقابتی | ۵ کلید `competitive_pricing` |
| Forex و کلَمپ Karlancer | `forex_enabled`، `soft_clamp`، نرخ مبنا، حساسیت، منبع، ضریب کلَمپ/هدف |

دو بخش آخر زیر **تنظیمات پیشرفته** (قابل بستن) قرار دارند.

### API

همه در [dashboard/urls.py](dashboard/urls.py). بدون احراز هویت — جزئیات در
[امنیت](#fa-security).

| مسیر | متد | نقش |
|---|---|---|
| `/` | GET | صفحهٔ اصلی |
| `/settings/` | GET | صفحهٔ تنظیمات (**توکن تلگرام و کلید AI در HTML آن است**) |
| `/api/status` | GET | وضعیت لحظه‌ای (سهمیه، متچ، بید، نرخ دلار، `has_cookies`) |
| `/api/events` | GET | رویدادهای جدید از آخرین `event_id` |
| `/api/logs` | GET | دُم لاگ با `since_byte` |
| `/api/start` | POST | شروع مانیتور در thread جدا |
| `/api/stop` | POST | توقف مانیتور |
| `/api/login` | POST | باز کردن پنجرهٔ کروم برای لاگین دستی |
| `/api/auth/request-otp` | POST | درخواست کد پیامک |
| `/api/auth/verify-otp` | POST | تأیید کد و ذخیرهٔ توکن |
| `/api/auth/otp-status` | GET | پیگیری نتیجهٔ دو مرحلهٔ بالا |
| `/api/login_done` | POST | پاک کردن پیام ورود (فقط UI) |
| `/api/dry_run` | POST | روشن/خاموش کردن dry-run |
| `/api/bid` | POST | ارسال دستی پیشنهاد (با `dry_run=true` رد می‌شود) |
| `/api/settings` | POST | خواندن و **بازنویسی کامل** `config.yaml` |
| `/api/test-ai` | POST | تست اتصال مدل |
| `/api/test-telegram` | POST | تست ارسال پیام |
| `/api/demo` | POST | اجرای موتور هیوریستیک روی دادهٔ آزمایشی |
| `/api/skills` | GET | کاتالوگ مهارت‌های پونیشا (از کش/API) |
| `/api/open_project` | POST | باز کردن صفحهٔ پروژه |

> درخواست‌های طولانی (شروع، لاگین، OTP، بید) با کد `202` برمی‌گردند و نتیجه را بعداً
> باید از `/api/status` یا `/api/auth/otp-status` گرفت.

---

<a id="fa-pricing"></a>
## قیمت نهایی چطور ساخته می‌شود

### موتور هیوریستیک (بدون AI)

ترتیب دقیق اجرا در `HeuristicBidDecision`:

1. اگر پروژه مهارت داشت و همپوشانی < `min_skill_match` → رد.
   اگر مهارتی نداشت، همپوشانی **نامشخص** تلقی می‌شود (نه صفر) و از دروازه رد می‌شود.
2. اگر `budget_min` بالای ۵۰ میلیون بود → رد.
3. قیمت = میانهٔ بازهٔ بودجه، بعد کلیپ بین `profile.min_budget` و `max_budget`.
4. تعدیل رقابت: پیشنهادها ≥ ۱۵ → **۱۵٪ تخفیف**؛ پیشنهادها < ۵ → **۱۰٪ افزایش**.
5. بج «فوری» → **۲۰٪ افزایش**. بج «متمایز/برجسته/حرفه‌ای» → **×۱٫۱۵**.
6. برگشت به بازهٔ بودجهٔ پروژه.
7. اگر بودجه ناشناخته بود → برآورد از بازه‌های `X-YM` داخل `profile.about`.
8. **کف:** `max(profile.min_budget, ۲,۰۰۰,۰۰۰)` و بعد `max(قیمت, budget_min)` —
   چون پونیشا هر پیشنهادی زیر حداقل اعلام‌شدهٔ کارفرما را رد می‌کند.
9. مدت = `suggested_days / 2` (حداقل ۱).

### موتور AI

1. دروازهٔ مهارت با `ai.min_skill_gate` (۰٫۲). اگر پروژه مهارتی نداشت، دروازه **رد می‌شود**
   و فقط از روی توضیحات قضاوت می‌شود.
2. اگر `ai.karlancer_enabled` باشد، دادهٔ بازار از karlancer.com جمع می‌شود (فقط-خواندنی).
3. فراخوانی مدل. خروجی JSON است با شکل دقیق:
   ```json
   {"should_bid": true, "price": 6500000, "duration_days": 21,
    "payment_steps": [{"title": "پرداخت کامل", "percent": 100}],
    "message": "متن پیشنهاد فارسی، حداکثر ۵۰۰ کاراکتر"}
   ```
   فیلد `message` همان متنی است که در فرم پونیشا جای‌گذاری می‌شود.
4. **افزایش فارکس:** `factor = 1 + (نرخ_فعلی − نرخ_مبنا) / نرخ_مبنا × sensitivity`
   و `factor` هرگز زیر ۱ نمی‌رود (با ریال قوی‌تر، زیر قیمت AI پایین نمی‌رویم).
   اگر نرخ در دسترس نباشد، صرفاً همین مرحله رد می‌شود — حمل‌ونقل متوقف نمی‌شود.
5. **کلَمپ نرم Karlancer:** اگر قیمت > میانگین بازار × `clamp_multiplier` →
   قیمت = میانگین بازار × `target_multiplier`.
6. گرد کردن به مضرب ۱۰۰٬۰۰۰ (حداقل ۱۰۰٬۰۰۰).
7. **کف لیستینگ:** قیمت به `budget_min` بالا کشیده می‌شود.
8. هر خطا یا JSON خراب → **بازگشت خودکار به موتور هیوریستیک** (پیشوند
   `(AI fallback)` روی پیام می‌آید). ربات هیچ‌وقت به خاطر AI از کار نمی‌ایستد.

قوانین کلیدی داخل system prompt مدل: قیمت از **بازار** می‌آید نه از بودجهٔ کارفرما؛
بودجهٔ کارفرما سقف نیست مگر اینکه «سقف قطعی» ذکر شده باشد؛ و پلهٔ پیچیدگی (خواندنی /
احراز هویت / انتشار زیر حساب کارفرما / دور زدن کپچا) قیمت را به بالای بازه می‌برد.

### یک مثال کامل، با همهٔ اعداد

یک پروژهٔ فرضی:

| ورودی | مقدار |
|---|---|
| بازهٔ بودجهٔ کارفرما | ۵٬۰۰۰٬۰۰۰ تا ۹٬۰۰۰٬۰۰۰ |
| بازهٔ شما (`bid.budget_min`/`budget_max`) | ۰ تا ۱۵٬۰۰۰٬۰۰۰ |
| مهارت پروژه | `پایتون (Python)` — با پروفایل ۱۰۰٪ همپوشانی |
| تعداد پیشنهاد رقبا | ۴ |
| بج | «فوری» |
| مهلت ارسال پروژه (`suggested_days`) | ۳۰ روز |

**الف) موتور هیوریستیک** (`ai.enabled: false`)، قدم‌به‌قدم:

| # | مرحله | محاسبه | نتیجه |
|---:|---|---|---:|
| ۱ | میانهٔ بازهٔ بودجه | `(۵٬۰۰۰٬۰۰۰ + ۹٬۰۰۰٬۰۰۰) ÷ ۲` | **۷٬۰۰۰٬۰۰۰** |
| ۲ | کلیپ به بازهٔ شما | داخل `۰ تا ۱۵٬۰۰۰٬۰۰۰` است → تغییری نمی‌کند | ۷٬۰۰۰٬۰۰۰ |
| ۳ | رقابت کم (۴ < ۵) | `× (۱ + ۱۰٪)` | **۷٬۷۰۰٬۰۰۰** |
| ۴ | بج «فوری» | `× (۱ + ۲۰٪)` | **۹٬۲۴۰٬۰۰۰** |
| ۵ | برگشت به بازهٔ بودجه | بیش از سقف کارفرما (۹٬۰۰۰٬۰۰۰) نمی‌توان گرفت | **۹٬۰۰۰٬۰۰۰** |
| ۶ | کف نهایی | `max(۹٬۰۰۰٬۰۰۰, max(۲٬۰۰۰٬۰۰۰, ۵٬۰۰۰٬۰۰۰))` → تغییری نمی‌دهد | **۹٬۰۰۰٬۰۰۰** |
| ۷ | مدت | `۳۰ ÷ ۲` | **۱۵ روز** |

> **خروجی: ۹٬۰۰۰٬۰۰۰ تومان / ۱۵ روز**
>
> نکتهٔ مهم این مثال: مرحلهٔ ۵ همهٔ افزایش‌های مراحل ۳ و ۴ را به سقفِ کارفرما برگرداند،
> پس قیمت نهایی دقیقاً سقفِ بودجه شد. اگر سقفِ کارفرما ۱۲٬۰۰۰٬۰۰۰ بود، عدد **۹٬۲۴۰٬۰۰۰**
> (بدون کلیپ) همان خروجی می‌شد.

**ب) موتور AI** — همان پروژه، با سه ورودی بیشتر:

| ورودی | مقدار |
|---|---|
| قیمت خامی که مدل برگرداند | ۸٬۰۰۰٬۰۰۰ |
| نرخ دلار / نرخ مبنا | ۲۳۵٬۹۰۲ / ۲۲۹٬۰۰۰ و `forex.sensitivity: 0.5` |
| میانگین بازار Karlancer | ۴٬۰۰۰٬۰۰۰ و `karlancer`: `clamp 3.0` / `target 2.5` |
| کف لیستینگ | ۵٬۰۰۰٬۰۰۰ |

| # | مرحله | محاسبه | نتیجه |
|---:|---|---|---:|
| ۱ | افزایش فارکس | `۱ + ۳٫۰۱٪ × ۰٫۵ = ۱٫۰۱۵۱` → `۸٬۰۰۰٬۰۰۰ × ۱٫۰۱۵۱` | **۸٬۱۲۰٬۷۹۹** |
| ۲ | کلَمپ Karlancer | سقف = `۴٬۰۰۰٬۰۰۰ × ۳٫۰ = ۱۲٬۰۰۰٬۰۰۰` و `۸٬۱۲۰٬۷۹۹ < سقف` → رد نمی‌شود | ۸٬۱۲۰٬۷۹۹ |
| ۳ | گرد به مضرب ۱۰۰٬۰۰۰ | `round(۸٬۱۲۰٬۷۹۹ ÷ ۱۰۰٬۰۰۰) × ۱۰۰٬۰۰۰` | **۸٬۱۰۰٬۰۰۰** |
| ۴ | کف لیستینگ | `max(۸٬۱۰۰٬۰۰۰, ۵٬۰۰۰٬۰۰۰)` → تغییری نمی‌دهد | **۸٬۱۰۰٬۰۰۰** |

> **خروجی: ۸٬۱۰۰٬۰۰۰ تومان**

حالا همان حالت را با میانگین بازار **۲٬۶۰۰٬۰۰۰** (مرحلهٔ ۲ فعال می‌شود):

```
سقف   = ۲٬۶۰۰٬۰۰۰ × ۳٫۰ = ۷٬۸۰۰٬۰۰۰
۸٬۰۰۰٬۰۰۰ > ۷٬۸۰۰٬۰۰۰  →  کلَمپ
قیمت  = ۲٬۶۰۰٬۰۰۰ × ۲٫۵ = ۶٬۵۰۰٬۰۰۰
گردکردن و کف لیستینگ (۵٬۰۰۰٬۰۰۰) تغییری نمی‌دهد
خروجی = ۶٬۵۰۰٬۰۰۰ تومان
```

> همهٔ اعداد بالا با **اجرای واقعی** `HeuristicBidDecision` و `LLMBidDecision` روی همین
> ورودی‌ها به‌دست آمده‌اند، نه با حساب دستی.

---

<a id="fa-security"></a>
## امنیت

> 🔴 **داشبورد هیچ احراز هویتی ندارد** و `GET /settings/` توکن ربات تلگرام و کلید API
> مدل را **به‌صورت متن ساده داخل HTML** می‌گذارد. هر کسی که به پورت دسترسی داشته باشد،
> هر دو را می‌بیند و با `POST /api/settings` می‌تواند آن‌ها را عوض کند.

پس:

- پیش‌فرض `127.0.0.1` را حفظ کنید. `--host 0.0.0.0` را فقط پشت **nginx با basic auth**
  یا **SSH tunnel** باز کنید — قالب‌ها در [deploy/nginx.conf.example](deploy/nginx.conf.example)
  و راهنمای کامل در [deploy/README-VPS.md](deploy/README-VPS.md) است.
- فایل‌هایی که **هرگز** نباید کامیت شوند (همه در `.gitignore`):

  | فایل | چرا |
  |---|---|
  | `ponisha.db` | کوکی‌های زندهٔ سشن — یعنی تصاحب کامل حساب |
  | `config.yaml` | توکن تلگرام و کلید AI |
  | `deploy/*.env*` | `SECRET_KEY` و بقیهٔ اسرار |
  | `bid_log.json` | تاریخچهٔ پیشنهادهای شما |
  | `data/`، `debug/`، `*.log` | کش و دیتای اجرا |

  نسخهٔ نمونهٔ امن آن‌ها track شده: [config.example.yaml](config.example.yaml)،
  [deploy/bot.env.example](deploy/bot.env.example).
- **`bid.dry_run` پیش‌فرض `true` است.** تا وقتی عمداً خاموشش نکرده‌اید، هیچ پیشنهادی
  فرستاده نمی‌شود. این تنها کلید ایمنی در برابر سوتفاهم است.
- `PONISHA_TOKEN_PASSPHRASE` را خالی نگذارید: مقدار خالیِ تعریف‌شده با «تعریف‌نشده»
  فرق دارد و در نسخه‌های قدیمی باعث می‌شد سرویس با کلید خالی رمزگذاری کند در حالی که
  اجرای دستی با کلید پیش‌فرض کار می‌کرد.

---

<a id="fa-deploy"></a>
## استقرار روی سرور

راهنمای گام‌به‌گام، شامل بسته‌های سیستمی، systemd، nginx و چک سلامت:

**[deploy/README-VPS.md](deploy/README-VPS.md)**

خلاصهٔ آن:

```bash
sudo cp deploy/ponishabot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ponishabot
```

یونیت systemd از `EnvironmentFile=/opt/ponishabot/deploy/bot.env` استفاده می‌کند،
`manage.py bot --host 127.0.0.1 --port 8000` را اجرا می‌کند و `TimeoutStopSec=180`
دارد تا یک چرخهٔ بید (سلنیوم + AI) تمام شود. `DJANGO_DEBUG=0` در تولید است و
`manage.py bot` با `insecure=True` از بی‌کار شدن استاتیک‌ها جلوگیری می‌کند.

> **ممنوع: بیش از یک پروسه.** این پروژه Django-WSGI نیست؛ یک پروسه است با یک رشتهٔ
> مانیتور و یک درایور سلنیوم. `gunicorn --workers N` با N بزرگ‌تر از ۱ یعنی **پیشنهاد
> تکراری**. اگر واقعاً به یک front-end جدا نیاز داری، یک worker و `--threads` تنها شکل
> امن است — ولی حتی همان هم بدون دلیل است، چون خود `runserver` کافی است.
>
> `gunicorn` در `requirements.txt` هست ولی جایی در کد یا `deploy/` استفاده نمی‌شود
> (سرویس با `manage.py bot` بالا می‌آید). اگر روی لینوکس نصبش نمی‌خواهی، همان خط را
> نصب‌نشده بگذار — ربات کارش را می‌کند.

---

<a id="fa-troubleshooting"></a>
## عیب‌یابی

| علامت | علت | راه‌حل |
|---|---|---|
| صفحه باز می‌شود ولی **هیچ دکمه‌ای کار نمی‌کند** | `DJANGO_DEBUG=0` بدون `insecure` → فایل‌های استاتیک 404 می‌شوند و `poll.js` اصلاً لود نمی‌شود | `curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/static/dashboard/js/poll.js` — اگر 404 بود، سرویس را با `manage.py bot` (نه `runserver` خام) بالا بیاورید |
| در لاگ «موفق» دیده می‌شود ولی پیشنهاد به پونیشا نرسیده | فیلدهای React با `value` پر می‌شوند نه با تایپ، یا سیگنال موفقیت به المان دیگری خورده | **همیشه با «پیشنهادهای من» در سایت تأیید کنید، نه با لاگ ربات.** لاگ فقط زمانی قابل اعتماد است که سایت هم تأیید کند |
| سرویس systemd کار نمی‌کند ولی اجرای دستی درست است | `PONISHA_TOKEN_PASSPHRASE=` در `deploy/bot.env` خالی است | یا کاملاً حذفش کنید، یا مقدار واقعی بگذارید |
| پیام «نشست رد شد (401)» یا `has_cookies: false` | کوکی یا توکن OTP منقضی شده | دوباره از داشبورد OTP بگیرید؛ نیازی به کروم نیست |
| «AI failed» در لاگ | endpoint یا کلید در دسترس نیست | نگران نباشید — خودکار به هیوریستیک برمی‌گردد. برای تست از **تست AI** در Settings استفاده کنید |
| نرخ دلار `—` نشان داده می‌شود | اتصال به Wallex قطع است | فقط همان افزایش قیمت رد می‌شود؛ بید ادامه پیدا می‌کند. `forex.enabled: false` کنید تا پیام تکراری نگیرید |
| ربات با پیام «سهمیه کم» متوقف شد | تا `min_remaining_proposals` رسیده | هدفمند است تا سهمیهٔ محدود سوخت نشود؛ اگر سهمیه را بالا بردید، `min_remaining_proposals` را هم کم کنید |
| پیشنهاد تکراری زد | چند پروسه یا چند نمونه همزمان | فقط یک نمونه. `ps` چک کنید و `bid_log.json` را ببینید کدام پروژه ثبت شده |
| ذخیرهٔ Settings چند بار «Saved» نشان می‌دهد | یک `POST /api/settings` چند بار `ConfigLoader.save` صدا می‌زند | عادی است، حلقهٔ خطا نیست |

---

<a id="fa-layout"></a>
## ساختار پوشه‌ها

```
main.py                  ورودی ترمینال (TUI)
ponishabot/tui/          خود TUI (صفحه‌ها، کلیدها، bridge منطق خالص)
manage.py                ورودی وب جنگو (`python manage.py bot`)
web/                     تنظیمات پروژهٔ جنگو
dashboard/               اپ وب: ویوها، تمپلیت‌ها، استاتیک
ponishabot/              هستهٔ ربات (جدول ماژول‌ها در بالا)
assets/fonts/            فونت وزیرمتن TTF برای رابط وب
deploy/                  systemd، nginx، قالب env، راهنمای VPS
descriptions.txt         متن پیشنهاد (برای مسیرهای هیوریستیک)
config.example.yaml      قالب تنظیمات — کپی به config.yaml
requirements.txt         وابستگی‌ها

# فقط-محلی (در gitignore)
config.yaml              تنظیمات واقعی شما
ponisha.db               کوکی‌های سشن
bid_log.json             تاریخچهٔ پیشنهادها
ponishabot.log           لاگ ربات
data/skills_cache.json   کش کاتالوگ مهارت‌ها
debug/                   اسکرین‌شات خطا و اسکریپت‌های آزمایشی
venv/                    محیط مجازی
```

---

<a id="en"></a>
# ponishabot (English)

An automation bot for [ponisha.ir](https://ponisha.ir) (Iranian freelance marketplace):
monitors newly posted projects, matches them against your skill profile, sends Telegram
alerts, and can auto-bid through Chrome (Selenium). A pluggable AI decision engine (any
OpenAI-compatible endpoint, e.g. 9router) decides whether to bid, at what price and
duration.

The web UI is a **local Django dashboard** styled after the 9Router panel.

**Language:** [فارسی](#fa) · English

**Contents:** [Architecture](#en-arch) · [Setup](#en-setup) · [Run](#en-run) ·
[Login](#en-login) · [Configuration](#en-config) · [Dashboard](#en-dashboard) ·
[Pricing](#en-pricing) · [Security](#en-security) · [Deployment](#en-deploy) ·
[Troubleshooting](#en-troubleshooting) · [Layout](#en-layout)

> ⚠️ **Three hard rules**
>
> 1. **Always one process.** The bot owns a singleton (monitor thread + Selenium
>    driver). Multiple workers or instances means **duplicate bids**.
> 2. **Never autoreload.** A reload kills the monitor thread and Chrome mid-run.
> 3. **The dashboard has no authentication.** Keep it on `127.0.0.1` — see
>    [Security](#en-security).

---

<a id="en-arch"></a>
## Architecture

One polling cycle looks like this:

```
scraper          reads listing + detail pages (HTML + the hidden __NEXT_DATA__ JSON)
   │
   ▼
monitor          loops: skill filter → priority queue (fresh listings first) → dedup
   │
   ▼
decision         should we bid? at what price / duration?
   ├─ LLMBidDecision           model + Karlancer market data + forex rate
   └─ HeuristicBidDecision     competitive pricing rules (no network)
   │
   ├──────────────► notifier   Telegram alert
   │
   ▼
bidder           fills and submits the proposal form with Selenium
                  (only when dry_run=false)
```

`botstate` keeps everything as a singleton, queues events and builds a
`status_snapshot` for the dashboard every 2 s; `app.py` wires the parts together.

### Modules

| File | LOC | Role |
|---|---:|---|
| [ponishabot/app.py](ponishabot/app.py) | 575 | Wiring: session, scraper, monitor, bidder, notifier |
| [ponishabot/auth.py](ponishabot/auth.py) | 625 | OTP + browser login, `pss-at` cookie, Chrome driver |
| [ponishabot/bidder.py](ponishabot/bidder.py) | 480 | Filling/submitting the proposal form, success detection |
| [ponishabot/botstate.py](ponishabot/botstate.py) | 326 | Singleton, event queue, status, quota, USD rate |
| [ponishabot/config.py](ponishabot/config.py) | 186 | Reading/writing `config.yaml` |
| [ponishabot/decision.py](ponishabot/decision.py) | 882 | Decision engines: LLM and heuristic |
| [ponishabot/forex.py](ponishabot/forex.py) | 89 | USDT/Toman rate from Wallex + uplift factor |
| [ponishabot/karlancer_scraper.py](ponishabot/karlancer_scraper.py) | 254 | Market comparison from karlancer.com (read-only) |
| [ponishabot/models.py](ponishabot/models.py) | 64 | `Project` / `Profile` / `BidPlan` |
| [ponishabot/monitor.py](ponishabot/monitor.py) | 269 | Poll loop, filtering, quota gate |
| [ponishabot/notifier.py](ponishabot/notifier.py) | 94 | Telegram alerts |
| [ponishabot/tui/](ponishabot/tui/) | 700 | Terminal UI: screens, keys, log filter |
| [ponishabot/paths.py](ponishabot/paths.py) | 23 | Every file path (anchored to the repo root, not CWD) |
| [ponishabot/scraper.py](ponishabot/scraper.py) | 550 | Listing/detail scraping, quota read |
| [ponishabot/skills.py](ponishabot/skills.py) | 120 | ponisha skill taxonomy (7-day cache) |
| [dashboard/](dashboard/) | 611 | Web views and URLs |
| [web/](web/) | 99 | Django project settings |

---

<a id="en-setup"></a>
## Setup

Requirements: **Python 3.10+** (Django 5) and **Google Chrome** on the same machine.

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

On Linux use `source venv/bin/activate` instead.

> **Known inconsistency:** [requirements.txt](requirements.txt) pins `django>=5.0,<6`,
> but the environment currently installed on this machine runs **Django 6.1.1 on
> Python 3.14.7**. A fresh `pip install -r requirements.txt` therefore downgrades Django
> from 6.1.1 to **5.2.17** and silently changes the stack you are running today. If you
> want to keep the installed Django, do not run that install blindly.
>
> The `django>=5.0,<6` pin is also incompatible with Django 6.x; pip prefers downgrading
> over refusing to install.

### Tests

```bash
python -m unittest discover -s tests -v
```

[`tests/`](tests/) needs no extra dependency (plain `unittest`) and is entirely
**offline**: no network call, no Selenium, no ponisha session. It targets the pure
functions — where the expensive mistakes live:

| Group | What it locks down |
|---|---|
| `ScraperText` | Persian digit normalization, budget parsing, and **carousel stripping** — so another project's budget cannot sit where the employer's belongs |
| `ForexUplift` | The dollar factor; in particular that a stronger rial never takes the price **under** the model's estimate |
| `HeuristicPricing` | The README's worked example (9,000,000 / 15 days), the listing floor, and that an untagged listing is not rejected |
| `AIPricingAdjustments` | The Karlancer clamp firing and not firing, rounding, and the raise to the floor |
| `LLMResponseParsing` | Malformed or fence-wrapped JSON must not raise (it has to fall back to the heuristic) |
| `PanelLogHandling` | Collapsing repeats (`Forex fetch failed ×9`), noise filtering, and that bot events survive the default view |
| `PanelFormatting` | Money/duration formatting and markup safety — a log message containing `[` must not crash rich |
| `TerminalSingleton` | Regression: `main.py` must not build a second `BotApp` (the panel would report on the wrong one) |
| `ConfigDefaults` | The defaults the README documents — including `dry_run: true` |

CI runs on every push and pull request
([.github/workflows/tests.yml](.github/workflows/tests.yml)) across Python 3.10 to 3.13.

> **Why these matter:** this project's history holds several bugs that all reported
> success while doing nothing — a budget read from the wrong element, a proposal sent
> under the employer's floor, a project recorded as done that was never bid on. These
> tests pin exactly those paths so they cannot come back.

---

<a id="en-run"></a>
## Run

```bash
python manage.py bot                # web dashboard → http://127.0.0.1:8000/
python main.py                      # interactive terminal UI
python main.py --plain              # logs only, no TUI
python manage.py bot_login          # open Chrome for a manual login
```

### `main.py` — terminal UI (TUI)

Does what the dashboard does, without opening a browser: live status, projects,
**every setting**, and **login** — all inside the terminal.

It opens on a **menu**, and the **monitor does not start on its own** — you pick
each step.

```
 ┌──────────────────────────────────────────────┐
 │  ponishabot                                  │
 │  ponisha.ir monitor                          │
 │                                              │
 │  ▸ [1]  Dashboard   live status and controls │
 │    [2]  Projects    scanned listings         │
 │    [3]  Settings    bot configuration        │
 │    [4]  Login       sign in to ponisha       │
 │    [5]  Log         live output              │
 │                                              │
 │    [q]  quit                                 │
 │                                              │
 │  arrows move · Enter opens · or the number   │
 └──────────────────────────────────────────────┘
```

> **The interface is English.** The web dashboard is Persian because it is read
> in a browser; this is read inside a terminal grid, where Persian is genuinely
> fragile — the glyphs join, so the space a string occupies on screen does not
> match its length and columns stop lining up. Data from ponisha (badges like
> «فوری», the site's own error strings) stays Persian because it has to match
> the site.

#### Screens

`Tab` and `Shift+Tab` cycle through these six:

| Screen | What it does |
|---|---|
| **Menu** | pick a screen with `1`..`5`, or `↑↓` + `Enter` |
| **Dashboard** | live status, recent proposals, start/stop |
| **Projects** | table of scanned projects; `Enter` shows the decision, `/` searches, `b` bids |
| **Settings** | every setting; `Tab` between fields, `Ctrl+S` saves, `Esc` reverts |
| **Login** | mobile → SMS code, or `o` for browser login |
| **Log** | live log; `f` level, `n` noise, `c` clear |

#### Keys

| Key | Action |
|---|---|
| `1`..`5` | (on the menu) jump straight to a screen |
| `Tab` / `Shift+Tab` | next / previous screen |
| `↑` `↓` or `k` `j` | move through lists |
| `Enter` | confirm / details / edit |
| `Esc` | back to the menu / cancel an edit |
| `m` | return to the menu from anywhere |
| `q` | quit (closes the Chrome driver) |

Per screen:

| Screen | Key | Action |
|---|---|---|
| Dashboard | `s` | start / stop the monitor |
| Dashboard | `d` | toggle `dry_run` |
| Projects | `Enter` | decision detail (price, rivals, reason) |
| Projects | `/` | focus the search box |
| Projects | `b` | bid manually (disabled while `dry_run`) |
| Settings | `Ctrl+S` | save to `config.yaml` |
| Settings | `Esc` | discard unsaved edits |
| Login | `Enter` | send the code, then verify it |
| Login | `o` | log in through Chrome |
| Log | `f` | cycle the level (important → debug → info) |
| Log | `n` | show / hide noise |
| Log | `c` | clear |

> **Why there is no numeric screen shortcut:** a digit bound at the app level
> never reaches the app while a text field has focus. It would therefore work on
> the dashboard and silently fail on the login screen — the one screen where
> typing a number is the whole point. `Tab` always arrives, so it is the only
> way between screens.

#### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--plain` | off | stream the log instead of the TUI |
| `--log-level` | `important` | initial log level |

> **The log is filtered, not dumped.** A healthy run is full of `Forex fetch
> failed` repeating endlessly, which buries everything else. The TUI collapses
> repeats into a count (`×9`) and hides known noise — in one real sample, **4,817
> raw lines became 303 readable rows**. `n` shows everything.

> **Settings persist.** `Ctrl+S` on the settings screen runs the same validation
> the dashboard does (bad values are refused, out-of-range ones are clamped) and
> writes `config.yaml`, so the last scan's settings are remembered.

> **Degrades instead of breaking.** With `textual` missing, or output piped to a
> file, it falls back to plain log streaming and says so — never an exception.

### `manage.py bot`

| Flag | Env var | Default | Meaning |
|---|---|---|---|
| `--host` | `BOT_HOST` | `127.0.0.1` | bind address; `0.0.0.0` only behind protection |
| `--port` | `BOT_PORT` | `8000` | port |

The command sets `--noreload` and `insecure=True` itself:

- **`--noreload` is mandatory** — a Django reload destroys the monitor thread and the
  Chrome window mid-run.
- **`insecure=True`** makes `/static/` serve even when `DJANGO_DEBUG=0`. Without it
  `app.css` and `poll.js` 404 and **no button works**: the page loads but is inert.

If `--host` is anything other than `127.0.0.1`/`localhost`, the command prints an
authentication warning.

---

<a id="en-login"></a>
## Logging in to ponisha

There are two paths:

| | **OTP** (recommended) | **Browser login** |
|---|---|---|
| Where | Login in the dashboard | Login button or `manage.py bot_login` |
| Needs Chrome/display | **No** — plain `requests` | **Yes** |
| What gets stored | a Bearer JWT in the `meta` table | a `pss-at` cookie |
| Best for | headless VPS | local machine with Chrome |

The OTP flow is two steps: mobile number → SMS code → verify. It uses no Selenium at
all, and the site cookie is later derived from that token, so the bot can run without a
browser.

`PonishaAuth.auth()` decides in this order:

1. Probe the stored cookie against the **REST API** (not the HTML page) — 401 means a
   dead session.
2. If there is no cookie, or it is dead, derive the `pss-at` cookie from the stored OTP
   token.
3. Only then open Chrome.

> **How to tell whether you are logged in:** check `has_cookies` in the dashboard status
> strip; it accepts either a browser cookie or an OTP token. Decrypting `pss-at` needs
> `PONISHA_TOKEN_PASSPHRASE` (the built-in default is used when it is unset).

---

<a id="en-config"></a>
## Configuration

Two layers: **environment variables** (secrets and Django settings) and **`config.yaml`**
(everything else).

<a id="en-env"></a>
### Environment variables

| Variable | Read from | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `config.py` | — | **Overrides** `telegram.bot_token` |
| `TELEGRAM_CHAT_ID` | `config.py` | — | **Overrides** `telegram.chat_id` |
| `DJANGO_SECRET_KEY` | `web/settings.py` | local-only placeholder | Django key |
| `DJANGO_DEBUG` | `web/settings.py` | `1` | set `0` in production |
| `DJANGO_ALLOWED_HOSTS` | `web/settings.py` | `127.0.0.1,localhost` | comma separated |
| `BOT_HOST` | `manage.py bot` | `127.0.0.1` | bind address |
| `BOT_PORT` | `manage.py bot` | `8000` | port |
| `PONISHA_TOKEN_PASSPHRASE` | `auth.py` | built-in default | decrypting the `pss-at` cookie |

> ⚠️ **A `.env` file is never loaded.** There is no `python-dotenv` in the dependencies
> and no hand-rolled parser. Creating a `.env` in the repo root **does nothing**.
>
> - **Locally:** set the variable in your shell — `set TELEGRAM_BOT_TOKEN=...` in cmd,
>   `$env:TELEGRAM_BOT_TOKEN="..."` in PowerShell, `export TELEGRAM_BOT_TOKEN=...` in bash.
> - **On a VPS under systemd:** use `EnvironmentFile=`. Template:
>   [deploy/bot.env.example](deploy/bot.env.example) — copy to `deploy/bot.env` and
>   `chmod 600`.

<a id="en-config-table"></a>
### Full `config.yaml` reference

Starting point: [config.example.yaml](config.example.yaml) —

```bash
cp config.example.yaml config.yaml
```

The **Default** column is what the code uses with no file present; the **Example** column
is what ships in `config.example.yaml`. They **differ in several places**, so they get
separate columns to keep you from guessing.

**`telegram`**

| Key | Default | Example | Meaning |
|---|---|---|---|
| `telegram.bot_token` | `""` | `REPLACE_WITH_BOTTOKEN_FROM_BOTFATHER` | Telegram bot token |
| `telegram.chat_id` | `""` | `REPLACE_WITH_YOUR_CHAT_ID` | Chat id |

**`search`**

| Key | Default | Example | Meaning |
|---|---|---|---|
| `search.skills` | `[]` | `Python`, `Django`, `پایتون (Python)`, `جنگو (Django)` | Your skills — must match ponisha's skill strings **exactly** (`طراحی لوگو`, `جنگو (Django)`). Also editable from Settings. |

**`monitor`**

| Key | Default | Example | Meaning |
|---|---:|---:|---|
| `monitor.refresh_interval` | **300** | **60** | seconds between polls. Clamped to 10–3600 by the dashboard. |
| `monitor.max_pages` | **24** | **3** | max listing pages per cycle |
| `monitor.priority_window_minutes` | 5 | 5 | projects posted within this many minutes are processed **first** |

**`bid`**

| Key | Default | Example | Meaning |
|---|---:|---:|---|
| `bid.budget_min` | 0 | 0 | Your price floor. Feeds `profile.min_budget` in the heuristic engine and builds the final floor: `max(this, 2,000,000)`. Setting `2000000` means no proposal ever goes below 2 M. **Not a project filter** — a low-budget project is not rejected, the price just does not drop. |
| `bid.budget_max` | **10000000** | **25000000** | Your price ceiling. Feeds `profile.max_budget` and clamps the **budget midpoint before any competition adjustment**. Heuristic engine only; the AI path is bound neither by your ceiling nor the employer's. |
| `bid.proposal_template` | `descriptions.txt` | `descriptions.txt` | proposal text file (heuristic path) |
| `bid.min_skill_match` | 0.5 | 0.5 | minimum skill overlap for the heuristic engine |
| `bid.dry_run` | `true` | `true` | `true` = notify only, nothing is ever submitted |
| `bid.min_remaining_proposals` | 2 | 2 | stop before the proposal quota reaches this |

**`bid.competitive_pricing`** — heuristic engine only

| Key | Default | Example | Meaning |
|---|---:|---:|---|
| `high_competition_threshold` | 15 | 15 | The "heavy competition" threshold. The discount applies when bid count **≥ this** (i.e. 15 bids or more). Between 5 and 14 bids none of these five keys fire at all. |
| `high_competition_discount_percent` | 15 | 15 | That discount: `price × (1 − 15 %)`. On 7,000,000 → **5,950,000**. |
| `low_competition_discount_percent` | 10 | 10 | When bids are **< 5** → price goes **up** (the name says discount; it is actually a low-competition premium). `price × (1 + 10 %)`: 7,000,000 → **7,700,000**. |
| `urgency_premium_percent` | 20 | 20 | Applied when the project carries the «فوری» badge: `price × (1 + 20 %)`. 7,000,000 → **8,400,000**. Runs **after** the competition discount/premium, so it lands on top of whatever competition produced. |
| `premium_badge_multiplier` | 1.15 | 1.15 | Applied for «متمایز», «برجسته» or «حرفه‌ای» badges: `price × 1.15`. 7,000,000 → **8,049,999** (one Toman short of 8,050,000, because the price goes through `int()`). |

**`browser`**

| Key | Default | Example | Meaning |
|---|---|---|---|
| `browser.headless` | `false` | `false` | `true` on a display-less VPS |
| `browser.show_browser` | `false` | **`true`** | open a live Chrome window next to the monitor |
| `browser.user_data_dir` | `null` | `null` | Chrome profile path (keeps the session across runs) |

**`ai`**

| Key | Default | Example | Meaning |
|---|---|---|---|
| `ai.enabled` | `false` | `false` | `false` = heuristic engine |
| `ai.base_url` | `""` | `https://your-router.example/v1` | OpenAI-compatible endpoint |
| `ai.api_key` | `""` | `REPLACE_WITH_YOUR_AI_API_KEY` | API key |
| `ai.model` | `""` | `your-model-name` | model name |
| `ai.min_skill_gate` | 0.2 | 0.2 | minimum skill overlap before calling the model |
| `ai.max_description_chars` | 4000 | 4000 | max description characters sent to the model |
| `ai.temperature` | 0.2 | 0.2 | model temperature |
| `ai.karlancer_enabled` | **`false`** | **`true`** | collect Karlancer market data before deciding |
| `ai.karlancer_max_pages` | 2 | 2 | max Karlancer pages |

**`forex`** — price uplift driven by the dollar

| Key | Default | Example | Meaning |
|---|---|---|---|
| `forex.enabled` | `true` | `true` | `false` = no dollar-driven uplift at all, the model's price stands as returned (the Karlancer clamp still runs). Affects the AI path only. |
| `forex.baseline_usd_rate` | **500000** | **229000** | The zero point of the calculation. With the example values: current rate 235,902 against baseline 229,000 → `(235,902 − 229,000) / 229,000 = +3.01 %`. You measure against **the baseline you configured**, not against zero. |
| `forex.sensitivity` | 0.5 | 0.5 | How much of that 3.01 % reaches the price. Formula: `1 + (3.01 % × sensitivity)`. At 0.5 → `1.0151` and 8,000,000 → **8,120,799**. At `1.0` → **8,240,800**. At `0` → unchanged. The factor never drops below `1.0`, so a stronger rial never pushes us under the model's price. |
| `forex.source` | `wallex` | `wallex` | The only implemented source. Any other value returns no rate, the uplift is skipped, and the bot keeps bidding (fail-open). |

**`karlancer`** — soft price clamp

| Key | Default | Example | Meaning |
|---|---:|---:|---|
| `karlancer.soft_clamp_enabled` | `true` | `true` | `false` = no ceiling, the AI price stands at whatever it is. It can only have an effect if `ai.karlancer_enabled` is also `true` and market data was actually collected. |
| `karlancer.clamp_multiplier` | 3.0 | 3.0 | Ceiling = market average × 3.0. With an average of 4,000,000 the ceiling is **12,000,000** and a price of 8,000,000 stays below it (untouched). With an average of 2,600,000 the ceiling is **7,800,000** and 8,000,000 goes over it → the next key fires. |
| `karlancer.target_multiplier` | 2.5 | 2.5 | When the ceiling is breached, the price moves **to average × 2.5** (not to the ceiling itself). With an average of 2,600,000: `2,600,000 × 2.5 = 6,500,000`, i.e. `8,000,000 → 6,500,000`. In the example above that survives the listing floor (5,000,000), so it stands. |

**`profile`**

| Key | Default | Example | Meaning |
|---|---|---|---|
| `profile.name` | `""` | `""` | display name |
| `profile.about` | `""` | `Describe your experience, rates, and bid rules for the AI.` | **the most important field for the AI**: personal rules, experience and rates. Anything written here is treated as a binding directive. The heuristic engine also reads `X-YM` ranges from this text to estimate a price. |
| `profile.hourly_min` | 0 | 0 | minimum hourly rate (Toman) |
| `profile.hourly_max` | 0 | 0 | maximum hourly rate (Toman) |

> **Saving from the dashboard:** `POST /api/settings` rewrites the whole `config.yaml`.
> Because `Config` fills defaults for any missing key, the file is always written in
> full — so **manual comments inside `config.yaml` are lost** when you save from Settings.

---

<a id="en-dashboard"></a>
## Dashboard

`http://127.0.0.1:8000/`

### Home page

- **Status strip:** remaining proposals, matches, bids, uptime, USD/Toman rate
- **Controls:** Start / Stop / login, demo, and the `dry_run` toggle
- **Projects card:** list + search + a detail panel (budget, competitors, badges, skills,
  description) with a manual **bid** button and **copy link**
- **Live log:** filtered and counted, read from `ponishabot.log`

### Settings page

| Section | Contains |
|---|---|
| Controls | `headless`, `show_browser`, `dry_run`, proposal file, Chrome profile path |
| Telegram | token, chat id + a **send test** button |
| Search & budget | skills (with a **skill picker** backed by ponisha's catalog), budget window, poll interval, priority window, min skill match, max pages, stop-at quota |
| Profile | name, AI instructions (`about`), hourly rates |
| AI engine | enable, `karlancer_enabled`, endpoint, key, model, skill gate, temperature, max chars, Karlancer pages + a **test AI** button |
| Competitive pricing | the 5 `competitive_pricing` keys |
| Forex & Karlancer clamp | `forex_enabled`, `soft_clamp`, baseline rate, sensitivity, source, clamp/target multipliers |

The last two sit under a collapsible **advanced settings** block.

### API

All in [dashboard/urls.py](dashboard/urls.py). **Unauthenticated** — see
[Security](#en-security).

| Path | Method | Purpose |
|---|---|---|
| `/` | GET | home page |
| `/settings/` | GET | settings page (**contains the Telegram token and AI key in the HTML**) |
| `/api/status` | GET | live status (quota, matches, bids, USD rate, `has_cookies`) |
| `/api/events` | GET | events since a given `event_id` |
| `/api/logs` | GET | log tail via `since_byte` |
| `/api/start` | POST | start the monitor in a background thread |
| `/api/stop` | POST | stop the monitor |
| `/api/login` | POST | open Chrome for a manual login |
| `/api/auth/request-otp` | POST | request an SMS code |
| `/api/auth/verify-otp` | POST | verify the code and store the token |
| `/api/auth/otp-status` | GET | poll both OTP steps |
| `/api/login_done` | POST | clear the login banner (UI only) |
| `/api/dry_run` | POST | toggle dry-run |
| `/api/bid` | POST | manual bid (refused while `dry_run=true`) |
| `/api/settings` | POST | read and **fully rewrite** `config.yaml` |
| `/api/test-ai` | POST | test model connectivity |
| `/api/test-telegram` | POST | test sending a message |
| `/api/demo` | POST | run the heuristic engine on mock data |
| `/api/skills` | GET | ponisha skill catalog (cache/API) |
| `/api/open_project` | POST | open the project page |

> Long-running requests (start, login, OTP, bid) return `202`; poll `/api/status` or
> `/api/auth/otp-status` for the result.

---

<a id="en-pricing"></a>
## How the final price is built

### Heuristic engine (no AI)

Exact order in `HeuristicBidDecision`:

1. If the project has skills and overlap < `min_skill_match` → reject.
   If it has no skills, overlap is treated as **unknown** (not zero) and passes the gate.
2. Reject if `budget_min` exceeds 50,000,000.
3. Price = midpoint of the budget range, then clamped to
   `profile.min_budget` / `max_budget`.
4. Competition adjustment: bids ≥ 15 → **15 % discount**; bids < 5 → **10 % uplift**.
5. «فوری» badge → **+20 %**. «متمایز»/«برجسته»/«حرفه‌ای» → **×1.15**.
6. Clamp back into the project's budget range.
7. Unknown budget → estimate from `X-YM` ranges found in `profile.about`.
8. **Floors:** `max(profile.min_budget, 2,000,000)`, then
   `max(price, budget_min)` — ponisha rejects anything below the employer's stated
   minimum.
9. Duration = `suggested_days / 2` (minimum 1).

### AI engine

1. Skill gate at `ai.min_skill_gate` (0.2). A project with **no skills** skips the gate
   entirely and is judged from the description alone.
2. If `ai.karlancer_enabled`, market data is collected from karlancer.com (read-only).
3. Call the model. The output is strict JSON:
   ```json
   {"should_bid": true, "price": 6500000, "duration_days": 21,
    "payment_steps": [{"title": "پرداخت کامل", "percent": 100}],
    "message": "Persian proposal text, max 500 characters"}
   ```
   The `message` field is what gets pasted into the ponisha bid form.
4. **Forex uplift:** `factor = 1 + (current − baseline) / baseline × sensitivity`, and
   `factor` never drops below 1.0 (a stronger rial never pushes us under the AI's
   estimate). If the rate is unavailable the step is skipped — bidding continues.
5. **Karlancer soft clamp:** if price > market average × `clamp_multiplier` →
   price = market average × `target_multiplier`.
6. Round to the nearest 100,000 (minimum 100,000).
7. **Listing floor:** the price is raised to `budget_min`.
8. Any error or malformed JSON → **automatic fallback to the heuristic engine** (the
   message gets an `(AI fallback)` prefix). The bot never stalls because of the AI.

Key rules in the model's system prompt: price comes from the **market**, never from the
employer's budget; the employer's budget is not a ceiling unless it says «سقف قطعی»; and
the complexity ladder (read-only / authentication / publishing under the employer's
account / defeating a captcha) pushes the price to the top of its band.

### A worked example, with every number

One hypothetical project:

| Input | Value |
|---|---|
| Employer budget range | 5,000,000 to 9,000,000 |
| Your range (`bid.budget_min` / `budget_max`) | 0 to 15,000,000 |
| Project skill | `پایتون (Python)` — 100 % overlap with the profile |
| Competing bids | 4 |
| Badge | «فوری» (urgent) |
| Proposal deadline (`suggested_days`) | 30 days |

**A) Heuristic engine** (`ai.enabled: false`), step by step:

| # | Step | Calculation | Result |
|---:|---|---|---:|
| 1 | Budget midpoint | `(5,000,000 + 9,000,000) ÷ 2` | **7,000,000** |
| 2 | Clamp to your range | already inside 0–15,000,000 → unchanged | 7,000,000 |
| 3 | Low competition (4 < 5) | `× (1 + 10 %)` | **7,700,000** |
| 4 | Urgent badge | `× (1 + 20 %)` | **9,240,000** |
| 5 | Back into the budget range | cannot exceed the employer's 9,000,000 | **9,000,000** |
| 6 | Final floors | `max(9,000,000, max(2,000,000, 5,000,000))` → unchanged | **9,000,000** |
| 7 | Duration | `30 ÷ 2` | **15 days** |

> **Output: 9,000,000 Toman / 15 days**
>
> The point of this example is step 5: it rolled back all of the uplift from steps 3 and 4
> against the employer's ceiling, so the final price is exactly that ceiling. Had the
> employer's ceiling been 12,000,000, the output would have been **9,240,000** (uncapped).

**B) AI engine** — the same project, three more inputs:

| Input | Value |
|---|---|
| Price returned by the model | 8,000,000 |
| USD rate / baseline | 235,902 / 229,000 with `forex.sensitivity: 0.5` |
| Karlancer market average | 4,000,000 with `karlancer`: `clamp 3.0` / `target 2.5` |
| Listing floor | 5,000,000 |

| # | Step | Calculation | Result |
|---:|---|---|---:|
| 1 | Forex uplift | `1 + 3.01 % × 0.5 = 1.0151` → `8,000,000 × 1.0151` | **8,120,799** |
| 2 | Karlancer clamp | ceiling = `4,000,000 × 3.0 = 12,000,000`, and `8,120,799 < ceiling` → not triggered | 8,120,799 |
| 3 | Round to 100,000 | `round(8,120,799 ÷ 100,000) × 100,000` | **8,100,000** |
| 4 | Listing floor | `max(8,100,000, 5,000,000)` → unchanged | **8,100,000** |

> **Output: 8,100,000 Toman**

Now the same run with a market average of **2,600,000** (step 2 fires):

```
ceiling = 2,600,000 × 3.0 = 7,800,000
8,000,000 > 7,800,000   →  clamped
price   = 2,600,000 × 2.5 = 6,500,000
rounding and the listing floor (5,000,000) change nothing
output  = 6,500,000 Toman
```

> Every number above came from actually running `HeuristicBidDecision` and
> `LLMBidDecision` against these inputs, not from hand arithmetic.

---

<a id="en-security"></a>
## Security

> 🔴 **The dashboard has no authentication**, and `GET /settings/` puts the Telegram bot
> token and the AI API key **in plain text inside the HTML**. Anyone who can reach the
> port sees both and can change them with `POST /api/settings`.

Therefore:

- Keep the `127.0.0.1` default. Expose `--host 0.0.0.0` only behind **nginx with basic
  auth** or an **SSH tunnel** — templates are in
  [deploy/nginx.conf.example](deploy/nginx.conf.example) and the full guide is
  [deploy/README-VPS.md](deploy/README-VPS.md).
- Files that must **never** be committed (all covered by `.gitignore`):

  | File | Why |
  |---|---|
  | `ponisha.db` | live session cookies = full account takeover |
  | `config.yaml` | Telegram token and AI key |
  | `deploy/*.env*` | `SECRET_KEY` and other secrets |
  | `bid_log.json` | your bid history |
  | `data/`, `debug/`, `*.log` | caches and runtime data |

  Safe, tracked templates: [config.example.yaml](config.example.yaml),
  [deploy/bot.env.example](deploy/bot.env.example).
- **`bid.dry_run` defaults to `true`.** Until you deliberately turn it off, no proposal
  is ever sent. That is the only guard against a misconfiguration.
- Do not leave `PONISHA_TOKEN_PASSPHRASE` blank: a defined-but-empty value is not the
  same as an unset one, and older versions made the service encrypt with an empty key
  while a manual run used the built-in default.

---

<a id="en-deploy"></a>
## Deploying to a server

Step-by-step guide covering system packages, systemd, nginx and health checks:

**[deploy/README-VPS.md](deploy/README-VPS.md)**

In short:

```bash
sudo cp deploy/ponishabot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ponishabot
```

The unit reads `EnvironmentFile=/opt/ponishabot/deploy/bot.env`, runs
`manage.py bot --host 127.0.0.1 --port 8000`, and sets `TimeoutStopSec=180` so a bid
cycle (Selenium + AI) can finish. Production runs `DJANGO_DEBUG=0`, and
`manage.py bot` passes `insecure=True` so static files stay alive.

> **Forbidden: more than one process.** This is not a Django-WSGI app — it is one process
> with one monitor thread and one Selenium driver. `gunicorn --workers N` with N above 1
> means **duplicate bids**. If you genuinely need a separate front end, one worker with
> `--threads` is the only safe shape — and even that is pointless here, since `runserver`
> already does the job.
>
> `gunicorn` sits in `requirements.txt` but is used nowhere in the code or in `deploy/`
> (the service runs `manage.py bot`). If you would rather not install it on Linux, leave
> that line uninstalled — the bot does not need it.

---

<a id="en-troubleshooting"></a>
## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Page loads but **no button does anything** | `DJANGO_DEBUG=0` without `insecure` → static files 404 and `poll.js` never loads | `curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/static/dashboard/js/poll.js` — if it is 404, start the service with `manage.py bot`, not a bare `runserver` |
| Log says success but ponisha received nothing | React inputs filled with `value` instead of typed into, or the success signal matched an unrelated element | **Always verify under "my proposals" on the site, never from the bot's own log.** The log is only trustworthy when the site agrees |
| systemd service fails but a manual run works | `PONISHA_TOKEN_PASSPHRASE=` in `deploy/bot.env` is empty | remove the line entirely, or set a real value |
| "session rejected (401)" or `has_cookies: false` | cookie or OTP token expired | take a new OTP from the dashboard — no Chrome needed |
| "AI failed" in the log | endpoint or key unreachable | it falls back to the heuristic automatically; use **Test AI** in Settings to verify |
| USD rate shows `—` | Wallex unreachable | only the uplift is skipped and bidding continues; set `forex.enabled: false` to silence it |
| Bot stops with "quota low" | reached `min_remaining_proposals` | intentional, so the limited quota is not burned; if you raised the quota, lower `min_remaining_proposals` too |
| Duplicate bids | several processes or instances | run exactly one. Check `ps` and inspect `bid_log.json` to see which projects are already recorded |
| Saving settings shows "Saved" several times | one `POST /api/settings` calls `ConfigLoader.save` multiple times | expected, not a retry loop |

---

<a id="en-layout"></a>
## Layout

```
main.py                  terminal entry (TUI)
ponishabot/tui/          the TUI itself (screens, keys, pure-logic bridge)
manage.py                Django web entry (`python manage.py bot`)
web/                     Django project settings
dashboard/               web app: views, templates, static
ponishabot/              bot core (see the module table above)
assets/fonts/            Vazirmatn TTFs for the web UI
deploy/                  systemd, nginx, env template, VPS guide
descriptions.txt         proposal text (heuristic path)
config.example.yaml      settings template — copy to config.yaml
requirements.txt         dependencies

# local only (gitignored)
config.yaml              your real settings
ponisha.db               session cookies
bid_log.json             bid history
ponishabot.log           bot log
data/skills_cache.json   skill catalog cache
debug/                   error screenshots and scratch scripts
venv/                    virtualenv
```
