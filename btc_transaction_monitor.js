require("dotenv").config();
const axios = require("axios");
const express = require("express");

const app = express();
app.use(express.json());

const MEMPOOL_API = "https://mempool.space/api"; // Bitcoin Mempool API

// Check Bitcoin Transaction Status
app.get("/check-tx/:txid", async (req, res) => {
  const { txid } = req.params;
  try {
    const response = await axios.get(`${MEMPOOL_API}/tx/${txid}`);
    res.json({ status: "✅ Transaction Found", data: response.data });
  } catch (error) {
    res.status(500).json({ status: "❌ Transaction Not Found", error: error.message });
  }
});

// Monitor Large Transactions
app.get("/monitor-large-txs", async (req, res) => {
  try {
    const response = await axios.get(`${MEMPOOL_API}/mempool/recent`);
  const largeTxs = response.data.filter((tx) => tx.fee > 0.01); // Detect High-Fee Transactions
  res.json({ large_transactions: largeTxs });
  } catch (error) {
    res.status(500).json({ status: "❌ Error Fetching Transactions", error: error.message });
  }
});

app.listen(5002, () => console.log("🚀 Bitcoin TX Monitor Running on Port 5002"));
