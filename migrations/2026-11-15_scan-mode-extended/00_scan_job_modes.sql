create table if not exists animego_scan_job_modes (
    job_id integer primary key references animego_scan_jobs(id) on delete cascade,
    mode text not null check (mode in ('extended', 'full_v2'))
);
