const express = require('express');
const qrcode = require('qrcode-terminal');
const fs = require('fs');
const { Client, LocalAuth } = require('whatsapp-web.js');

const app = express();
app.use(express.json());

let isReady = false;
let currentQr = null;

// Auto-detect installed Chrome or Edge executable on Windows
function getChromeExecutable() {
    const candidates = [
        'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
        'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
        'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
        'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'
    ];
    for (const path of candidates) {
        if (fs.existsSync(path)) {
            return path;
        }
    }
    return undefined;
}

const chromePath = getChromeExecutable();
console.log(`[GATEWAY] Browser binary: ${chromePath || 'Default Puppeteer'}`);

const client = new Client({
    authStrategy: new LocalAuth({ dataPath: './.wwebjs_auth' }),
    webVersionCache: {
        type: 'remote',
        remotePath: 'https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/2.2412.54.html',
    },
    puppeteer: {
        headless: true,
        executablePath: chromePath,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-accelerated-2d-canvas',
            '--no-first-run',
            '--no-zygote',
            '--disable-gpu',
            '--disable-blink-features=AutomationControlled'
        ],
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    }
});

client.on('qr', (qr) => {
    currentQr = qr;
    console.log('\n================================================================');
    console.log('SCAN THIS QR CODE (or open http://127.0.0.1:3000 in your browser):');
    console.log('================================================================\n');
    qrcode.generate(qr, { small: false });
    console.log('\nTip: If terminal scanning says "try again", open:');
    console.log('👉 http://127.0.0.1:3000');
    console.log('in your browser to scan a crisp, high-resolution QR code!\n');
});

client.on('ready', () => {
    isReady = true;
    currentQr = null;
    console.log('\n================================================================');
    console.log('>>> WhatsApp Web Gateway is CONNECTED and READY! <<<');
    console.log('Listening for OTP requests on http://127.0.0.1:3000/send-otp');
    console.log('================================================================\n');
});

client.on('authenticated', () => {
    console.log('[GATEWAY] Authentication successful!');
});

client.on('auth_failure', (msg) => {
    console.error('[GATEWAY] Authentication failure:', msg);
});

client.on('disconnected', (reason) => {
    isReady = false;
    currentQr = null;
    console.log('[GATEWAY] Client was disconnected:', reason);
    client.initialize();
});

// Browser Web UI for easy high-resolution QR Code scanning
app.get('/', (req, res) => {
    if (isReady) {
        return res.send(`
            <!DOCTYPE html>
            <html>
            <head><title>NEXORA WhatsApp Gateway</title><meta name="viewport" content="width=device-width, initial-scale=1"></head>
            <body style="background:#070a10; color:#fff; font-family:system-ui,sans-serif; display:grid; place-items:center; min-height:100vh; margin:0;">
                <div style="background:#0e131c; border:1px solid #1f2937; padding:40px; border-radius:18px; text-align:center; max-width:440px;">
                    <div style="font-size:48px;">✅</div>
                    <h2 style="color:#d9ff5c; margin:15px 0 5px;">Gateway Connected!</h2>
                    <p style="color:#94a3b8; font-size:14px;">WhatsApp Web is linked and ready to send OTPs.</p>
                </div>
            </body>
            </html>
        `);
    }

    if (!currentQr) {
        return res.send(`
            <!DOCTYPE html>
            <html>
            <head><meta http-equiv="refresh" content="2"><title>Loading QR...</title></head>
            <body style="background:#070a10; color:#fff; font-family:system-ui,sans-serif; display:grid; place-items:center; min-height:100vh; margin:0;">
                <p style="color:#94a3b8;">Initializing WhatsApp Web... Please wait a few seconds.</p>
            </body>
            </html>
        `);
    }

    const qrImageUrl = `https://api.qrserver.com/v1/create-qr-code/?size=350x350&margin=15&data=${encodeURIComponent(currentQr)}`;
    res.send(`
        <!DOCTYPE html>
        <html>
        <head>
            <title>Scan WhatsApp QR Code</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <meta http-equiv="refresh" content="20">
            <style>
                body { background: #070a10; color: #f4f7fb; font-family: system-ui, -apple-system, sans-serif; display: grid; place-items: center; min-height: 100vh; margin: 0; text-align: center; }
                .card { background: #0e131c; border: 1px solid #1f2937; padding: 36px 30px; border-radius: 20px; box-shadow: 0 20px 60px rgba(0,0,0,0.5); max-width: 420px; }
                .qr-box { background: #ffffff; padding: 14px; border-radius: 16px; display: inline-block; margin: 20px 0; }
                .steps { text-align: left; background: #121824; border-radius: 12px; padding: 14px 18px; font-size: 13px; color: #cbd5e1; line-height: 1.6; }
                .steps ol { margin: 0; padding-left: 20px; }
            </style>
        </head>
        <body>
            <div class="card">
                <h2 style="margin:0 0 6px; letter-spacing:-0.03em;">Link WhatsApp</h2>
                <p style="color:#94a3b8; font-size:13px; margin:0 0 16px;">Scan this crisp QR code from your phone screen:</p>
                
                <div class="qr-box">
                    <img src="${qrImageUrl}" width="320" height="320" alt="WhatsApp QR Code" style="display:block;">
                </div>

                <div class="steps">
                    <ol>
                        <li>Open <strong>WhatsApp</strong> on your phone</li>
                        <li>Tap <strong>Settings</strong> or <strong>⋮</strong> &gt; <strong>Linked Devices</strong></li>
                        <li>Tap <strong>Link a Device</strong> and point your camera here</li>
                    </ol>
                </div>
                <small style="color:#64748b; font-size:11px; display:block; margin-top:14px;">This page refreshes automatically when a new QR code is generated.</small>
            </div>
        </body>
        </html>
    `);
});

app.post('/send-otp', async (req, res) => {
    if (!isReady) {
        return res.status(503).json({ ok: false, error: 'WhatsApp gateway is not connected yet. Please scan the QR code first.' });
    }
    const { phone, message } = req.body;
    if (!phone || !message) {
        return res.status(400).json({ ok: false, error: 'Missing phone or message parameter.' });
    }
    try {
        const cleanPhone = phone.replace(/\D/g, '');
        const chatId = `${cleanPhone}@c.us`;
        const result = await client.sendMessage(chatId, message);
        console.log(`[GATEWAY] OTP sent successfully to ${chatId}`);
        res.json({ ok: true, id: result.id ? result.id._serialized : 'sent' });
    } catch (err) {
        console.error('[GATEWAY] Error sending message:', err);
        res.status(500).json({ ok: false, error: err.message });
    }
});

app.get('/status', (req, res) => {
    res.json({ ready: isReady });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, '127.0.0.1', () => {
    console.log(`Starting WhatsApp Web Gateway on http://127.0.0.1:${PORT}...`);
    client.initialize();
});
