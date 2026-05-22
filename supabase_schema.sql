-- Run this in Supabase SQL Editor before deploying the Supabase version.

create table if not exists public.settings (
    key text primary key,
    value text not null default '',
    updated_at timestamptz not null default now()
);

create table if not exists public.payments (
    id bigint generated always as identity primary key,
    phone text not null,
    amount integer not null default 0,
    plan text not null default 'monthly',
    device_id text default '',
    merchant_request_id text default '',
    checkout_request_id text unique,
    status text not null default 'pending',
    result_code integer,
    result_desc text default '',
    mpesa_receipt text default '',
    username text default '',
    temp_password text default '',
    created_at timestamptz not null default now(),
    paid_at timestamptz
);

create table if not exists public.users (
    id bigint generated always as identity primary key,
    username text unique not null,
    phone text not null,
    password_hash text not null,
    status text not null default 'active',
    plan text not null default 'monthly',
    license_expiry timestamptz,
    device_id text default '',
    token text unique not null,
    must_change_password boolean not null default true,
    created_at timestamptz not null default now(),
    last_login timestamptz
);

create index if not exists idx_payments_checkout on public.payments(checkout_request_id);
create index if not exists idx_payments_phone on public.payments(phone);
create index if not exists idx_users_username on public.users(username);
create index if not exists idx_users_token on public.users(token);
