-- Tweet Pipeline Pro FINAL Supabase schema
-- Run in Supabase SQL Editor. Safe to run multiple times.

create table if not exists settings (
  key text primary key,
  value text not null default '',
  updated_at text not null default now()::text
);

create table if not exists payments (
  id bigserial primary key,
  phone text not null,
  amount integer not null,
  plan text not null default 'monthly',
  device_id text default '',
  ip_address text default '',
  merchant_request_id text default '',
  checkout_request_id text unique,
  status text not null default 'pending',
  result_code integer,
  result_desc text default '',
  mpesa_receipt text default '',
  username text default '',
  temp_password text default '',
  created_at text not null default now()::text,
  paid_at text
);

create table if not exists users (
  id bigserial primary key,
  username text unique not null,
  phone text not null,
  password_hash text not null,
  status text not null default 'active',
  plan text not null default 'monthly',
  license_expiry text,
  device_id text default '',
  ip_address text default '',
  token text unique not null,
  must_change_password boolean not null default true,
  created_at text not null default now()::text,
  last_login text
);

create table if not exists manual_requests (
  id bigserial primary key,
  request_id text unique not null,
  phone text not null,
  amount integer not null,
  plan text not null default 'monthly',
  device_id text default '',
  ip_address text default '',
  message text default '',
  status text not null default 'pending',
  username text default '',
  temp_password text default '',
  created_at text not null default now()::text,
  approved_at text,
  admin_note text default ''
);

-- Add columns for users/projects upgraded from older versions.
alter table payments add column if not exists ip_address text default '';
alter table users add column if not exists ip_address text default '';
alter table manual_requests add column if not exists ip_address text default '';


create table if not exists user_configs (
  id bigserial primary key,
  username text unique not null,
  config jsonb not null default '{}'::jsonb,
  updated_at text not null default now()::text
);
