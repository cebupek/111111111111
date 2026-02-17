const express = require("express");
const fetch = require("node-fetch"); // можно убрать на Node 18+

const app = express();
const PORT = process.env.PORT || 3000;

// ====== РОУТЫ ======

app.get("/", (req, res) => {
  res.send("✅ Server is alive");
});

app.get("/ping", (req, res) => {
  res.json({
    status: "ok",
    time: new Date().toISOString()
  });
});

// ====== ЗАПУСК СЕРВЕРА ======

app.listen(PORT, () => {
  console.log(`🚀 Server running on port ${PORT}`);
});

// ====== АВТО-ПИНГ КАЖДЫЕ 10 МИН ======

const SELF_URL = process.env.SELF_URL || `http://localhost:${PORT}`;

setInterval(async () => {
  try {
    const res = await fetch(`${SELF_URL}/ping`);
    console.log("🔁 Self ping:", res.status);
  } catch (err) {
    console.error("❌ Ping error:", err.message);
  }
}, 10 * 60 * 1000); // 10 минут
