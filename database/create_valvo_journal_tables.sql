-- Nexus Trading Journal — Core trades table
-- Run this in Supabase SQL Editor
-- Table name: journal_trades (used by backend journal_routes.py)

CREATE TABLE IF NOT EXISTS journal_trades (
  id SERIAL PRIMARY KEY,
  trade_no INTEGER,
  trade_date DATE NOT NULL,
  chart_image TEXT,
  symbol VARCHAR(50) NOT NULL DEFAULT '',
  name VARCHAR(100) DEFAULT '',
  setup JSONB DEFAULT '[]',
  entry_type VARCHAR(20) DEFAULT 'BREAKOUT',
  self_rating SMALLINT DEFAULT 3 CHECK (self_rating BETWEEN 0 AND 5),
  buy_sell VARCHAR(4) DEFAULT 'Buy',
  sector VARCHAR(100) DEFAULT '',
  security_id VARCHAR(50) DEFAULT '',

  -- Entry
  entry_price NUMERIC(12,2),
  avg_entry NUMERIC(12,2),
  sl NUMERIC(12,2),
  initial_qty INTEGER DEFAULT 0,

  -- Partial exits
  p1_price NUMERIC(12,2),
  p1_qty INTEGER DEFAULT 0,
  p1_date DATE,
  p1_sl NUMERIC(12,2),
  p2_price NUMERIC(12,2),
  p2_qty INTEGER DEFAULT 0,
  p2_date DATE,
  p2_sl NUMERIC(12,2),
  tsl NUMERIC(12,2),

  -- Full exits
  e1_price NUMERIC(12,2),
  e1_qty INTEGER DEFAULT 0,
  e1_date DATE,
  e2_price NUMERIC(12,2),
  e2_qty INTEGER DEFAULT 0,
  e2_date DATE,
  e3_price NUMERIC(12,2),
  e3_qty INTEGER DEFAULT 0,
  e3_date DATE,

  -- Qualitative
  plan_followed VARCHAR(12) DEFAULT '',
  exit_trigger TEXT DEFAULT '',
  growth_areas TEXT DEFAULT '',
  base_duration VARCHAR(50) DEFAULT '',
  notes TEXT DEFAULT '',

  -- Meta
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_journal_trades_date ON journal_trades(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_journal_trades_symbol ON journal_trades(symbol);
