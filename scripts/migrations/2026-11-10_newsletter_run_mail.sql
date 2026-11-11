-- Newsletter runs record what the mailer did with the issue after the publisher
-- put it on the web (NEWSLETTER_MAILER=resend). Idempotent.
alter table public.newsletter_runs add column if not exists mail_id text;
alter table public.newsletter_runs add column if not exists mail_status text;
alter table public.newsletter_runs add column if not exists mail_error text;
