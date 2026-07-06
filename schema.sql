-- SIGNAL — Supabase schema
-- Run this in the Supabase SQL editor.

create extension if not exists "uuid-ossp";

-- ---------------------------------------------------------------
-- users: profile data (linked to Supabase Auth's auth.users)
-- ---------------------------------------------------------------
create table if not exists users (
  id uuid primary key references auth.users(id) on delete cascade,
  nickname text not null,
  photo_url text,
  intro text,
  greeting_message text,
  is_traveler boolean default false,
  travel_city text,
  travel_date date,
  created_at timestamptz default now()
);

-- ---------------------------------------------------------------
-- presence: last-known city + coords, refreshed by client heartbeat
-- ---------------------------------------------------------------
create table if not exists presence (
  user_id uuid primary key references users(id) on delete cascade,
  city text not null,
  country text,
  lat double precision not null,
  lng double precision not null,
  last_seen timestamptz default now()
);

-- ---------------------------------------------------------------
-- match_requests: one row per "I'd like to chat" request
-- ---------------------------------------------------------------
create table if not exists match_requests (
  id uuid primary key default uuid_generate_v4(),
  from_user_id uuid references users(id) on delete cascade,
  to_user_id uuid references users(id) on delete cascade,
  status text not null default 'pending', -- pending | accepted | rejected
  created_at timestamptz default now()
);

-- ---------------------------------------------------------------
-- chat_rooms: created once a match_request is accepted
-- ---------------------------------------------------------------
create table if not exists chat_rooms (
  id uuid primary key default uuid_generate_v4(),
  match_request_id uuid references match_requests(id) on delete cascade,
  user_a uuid references users(id) on delete cascade,
  user_b uuid references users(id) on delete cascade,
  created_at timestamptz default now()
);

-- ---------------------------------------------------------------
-- businesses: placeholder for the future travel-guide expansion
-- (hotels / restaurants / attractions with paid listings). Not
-- used by the v1 app yet — kept here just so the schema doesn't
-- need a breaking migration later.
-- ---------------------------------------------------------------
create table if not exists businesses (
  id uuid primary key default uuid_generate_v4(),
  name text not null,
  category text, -- restaurant | hotel | attraction
  city text,
  lat double precision,
  lng double precision,
  is_sponsored boolean default false,
  created_at timestamptz default now()
);

-- ---------------------------------------------------------------
-- active_cities(): aggregates presence into one row per city,
-- counting only users seen in the last 2 minutes. This is what
-- the globe's markers are driven by.
-- ---------------------------------------------------------------
create or replace function active_cities()
returns table (city text, country text, lat double precision, lng double precision, online_count bigint)
language sql
as $$
  select
    city,
    max(country) as country,
    avg(lat) as lat,
    avg(lng) as lng,
    count(*) as online_count
  from presence
  where last_seen > now() - interval '2 minutes'
  group by city;
$$;
