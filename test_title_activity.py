import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import scrape_animego
import scrape_yummyanime
import sync_videos
import title_activity
from scripts import refresh_title_activity


class TitleActivityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = scrape_animego.init_db(Path(self.tmp.name) / "activity.sqlite")
        self.addCleanup(self.con.close)

    def add_title(self, anime_id, source):
        self.con.execute(
            "insert into anime(id,title,url,source,source_id,scraped_at) values (?,?,?,?,?,?)",
            (anime_id, f"Title {anime_id}", f"https://example.test/{anime_id}", source,
             str(anime_id), "2026-01-01T00:00:00+00:00"),
        )

    def activity(self, anime_id):
        return self.con.execute(
            "select last_changed_at,source_updated_at,source_checked_at from title_content_activity where anime_id=?",
            (anime_id,),
        ).fetchone()

    def test_all_sources_record_real_additions_but_not_rechecks_or_url_rotation(self):
        for anime_id, source in [(1, "animego"), (2, "yummyanime"), (3, "kinogoo")]:
            self.add_title(anime_id, source)
            self.con.execute(
                "insert into episodes(id,anime_id,number,has_video,scraped_at) values (?,?,?,1,?)",
                (anime_id * 100, anime_id, "1", "2026-02-01T00:00:00+00:00"),
            )
            self.assertEqual(self.activity(anime_id)[0], "2026-02-01T00:00:00+00:00")
            self.con.execute(
                "update episodes set scraped_at=? where id=?",
                ("2026-03-01T00:00:00+00:00", anime_id * 100),
            )
            self.assertEqual(self.activity(anime_id)[0], "2026-02-01T00:00:00+00:00")
            self.con.execute(
                "insert into video_sources(anime_id,episode_id,provider_id,translation_id,embed_url,scraped_at) "
                "values (?,?,?,1,?,?)",
                (anime_id, anime_id * 100, "provider", "https://player.example/old", "2026-04-01T00:00:00+00:00"),
            )
            self.assertEqual(self.activity(anime_id)[0], "2026-04-01T00:00:00+00:00")
            self.con.execute(
                "update video_sources set embed_url=?,scraped_at=? where anime_id=?",
                ("https://player.example/new-token", "2026-05-01T00:00:00+00:00", anime_id),
            )
            self.assertEqual(self.activity(anime_id)[0], "2026-04-01T00:00:00+00:00")

    def test_first_playable_provider_and_rollback_update_atomically(self):
        self.add_title(1, "kinogoo")
        self.con.execute(
            "insert into episodes(id,anime_id,number,has_video,scraped_at) values (100,1,'1',0,?)",
            ("2026-02-01T00:00:00+00:00",),
        )
        self.con.execute(
            "insert into video_sources(anime_id,episode_id,provider_id,translation_id,embed_url,scraped_at) "
            "values (1,100,'placeholder',1,null,?)", ("2026-02-01T00:00:00+00:00",),
        )
        self.con.commit()
        self.con.execute("begin")
        self.con.execute(
            "update video_sources set embed_url=?,scraped_at=? where anime_id=1",
            ("https://player.example/ready", "2026-04-01T00:00:00+00:00"),
        )
        self.assertEqual(self.activity(1)[0], "2026-04-01T00:00:00+00:00")
        self.con.rollback()
        self.assertEqual(self.activity(1)[0], "2026-02-01T00:00:00+00:00")

    def test_source_dates_remain_separate_from_local_changes_and_never_regress(self):
        self.add_title(1, "animego")
        now = dt.datetime(2026, 11, 15, 12, tzinfo=dt.timezone.utc)
        title_activity.record_source_date(self.con, 1, "2026-09-01T09:00:00+00:00", now)
        title_activity.record_source_date(self.con, 1, "2026-08-01T09:00:00+00:00", now)
        local, source, checked = self.activity(1)
        self.assertEqual(local, "2026-01-01T00:00:00+00:00")
        self.assertEqual(source, "2026-09-01T09:00:00+00:00")
        self.assertEqual(checked, "2026-11-15T12:00:00+00:00")
        with self.assertRaises(ValueError):
            title_activity.record_source_date(self.con, 1, "2030-01-01T00:00:00+00:00", now)

    def test_sitemaps_accept_only_valid_source_pages_and_dates(self):
        now = dt.datetime(2026, 11, 15, 12, tzinfo=dt.timezone.utc)
        animego = b"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://animego.me/anime/show-1</loc><lastmod>2026-11-10T01:00:00+03:00</lastmod></url>
          <url><loc>https://evil.example/anime/show-2</loc><lastmod>2026-11-10</lastmod></url>
          <url><loc>https://animego.me/anime/show-3</loc><lastmod>2030-01-01</lastmod></url>
          <url><loc>https://animego.me/anime/show-4</loc><lastmod>invalid</lastmod></url>
        </urlset>"""
        yummy = b"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://ru.yummyani.me/catalog/item/one-2026-11-15</loc><lastmod>2026-11-09</lastmod></url>
          <url><loc>https://evil.example/catalog/item/two</loc><lastmod>2026-11-09</lastmod></url>
        </urlset>"""
        self.assertEqual(title_activity.parse_animego_sitemap(animego, now), {1: "2026-11-09T22:00:00+00:00"})
        self.assertEqual(title_activity.parse_yummy_sitemap(yummy, now), {"one-2026-11-15": "2026-11-09T00:00:00+00:00"})
        with self.assertRaises(ValueError):
            title_activity.parse_animego_sitemap(b"<!DOCTYPE x [<!ENTITY a 'x'>]><urlset/>", now)

    def test_refresh_matches_exact_animego_ids_and_unique_yummy_slugs(self):
        self.add_title(1, "animego")
        self.add_title(20_000_001, "yummyanime")
        self.add_title(20_000_002, "yummyanime")
        self.add_title(20_000_003, "yummyanime")
        self.con.execute("update anime set url='https://ru.yummyani.me/catalog/item/one' where id=20000001")
        self.con.execute("update anime set url='https://ru.yummyani.me/catalog/item/ambiguous' where id=20000002")
        self.con.execute("update anime set url='https://ru.yummyani.me/catalog/item/exact-2026-11-15' where id=20000003")
        now = dt.datetime(2026, 11, 15, 12, tzinfo=dt.timezone.utc)
        snapshots = {
            "animego": {1: "2026-11-10T00:00:00+00:00", 20000002: "2026-11-11T00:00:00+00:00"},
            "yummyanime": {
                "one-2026-11-15": "2026-11-09T00:00:00+00:00",
                "ambiguous-2026-11-15": "2026-11-09T00:00:00+00:00",
                "ambiguous-2026-11-14": "2026-11-08T00:00:00+00:00",
                "exact-2026-11-15": "2026-11-07T00:00:00+00:00",
            },
        }
        count = title_activity.apply_source_snapshots(self.con, snapshots, now)
        self.assertEqual(count, 3)
        self.assertEqual(self.activity(1)[1], "2026-11-10T00:00:00+00:00")
        self.assertEqual(self.activity(20_000_001)[1], "2026-11-09T00:00:00+00:00")
        self.assertIsNone(self.activity(20_000_002)[1])
        self.assertEqual(self.activity(20_000_003)[1], "2026-11-07T00:00:00+00:00")

    def test_yummy_detail_records_video_date_even_when_no_new_content_is_written(self):
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        date = now - dt.timedelta(days=5)
        payload = {"anime_id": 1, "anime_url": "fixture", "title": "Fixture", "episodes": {"count": 1},
                   "videos": [{"number": 1, "date": int(date.timestamp())},
                              {"number": 2, "date": int((now + dt.timedelta(days=10)).timestamp())}]}
        item, detail, _episodes, _providers = scrape_yummyanime.parse_modern_payload(
            payload, "https://ru.yummyani.me/catalog/item/fixture")
        self.assertEqual(detail["source_updated_at"], date.isoformat())
        self.add_title(item["id"], "yummyanime")
        title_activity.record_detail_date(self.con, item, detail, now)
        self.assertIsNone(self.activity(item["id"])[1])
        self.con.execute("update anime set source_id='yummyani:1' where id=?", (item["id"],))
        args = sync_videos.parse_args(["--mode", "manual", "--yummy-ref", "fixture"])
        args.scraped_at = now.isoformat()
        with patch.object(sync_videos, "parse_yummy_detail_for_sync", return_value=(item, detail, [], [])):
            sync_videos.sync_yummy_title(self.con, "fixture", args, sync_videos.defaultdict(int), "manual")
        local, source, _checked = self.activity(item["id"])
        self.assertEqual(local, "2026-01-01T00:00:00+00:00")
        self.assertEqual(source, date.isoformat())

    def test_yummy_feed_refreshes_known_video_dates_and_honors_dry_run(self):
        self.add_title(20_000_001, "yummyanime")
        self.con.execute("update anime set source_id='yummyani:1' where id=20000001")
        self.con.commit()
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        date = now - dt.timedelta(days=2)
        payload = {"response": {"new_videos": [{"anime_id": 1, "date": int(date.timestamp()),
                                                 "player_title": "Alloha", "video_id": 1}]}}
        args = sync_videos.parse_args(["--mode", "hourly"])
        args.scraped_at = now.isoformat()
        args.dry_run = True
        with patch.object(sync_videos, "fetch_json_url", return_value=payload):
            sync_videos.sync_yummy_feed(self.con, args, sync_videos.defaultdict(int))
        self.assertIsNone(self.activity(20_000_001)[1])
        args.dry_run = False
        with patch.object(sync_videos, "fetch_json_url", return_value=payload):
            sync_videos.sync_yummy_feed(self.con, args, sync_videos.defaultdict(int))
        self.assertEqual(self.activity(20_000_001)[1], date.isoformat())

    def test_refresh_script_updates_dates_on_the_existing_database(self):
        self.add_title(1, "animego")
        self.con.commit()
        snapshots = {"animego": {1: "2026-09-01T00:00:00+00:00"}, "yummyanime": {}}
        with patch.object(title_activity, "fetch_source_snapshots", return_value=snapshots):
            count = refresh_title_activity.refresh(Path(self.tmp.name) / "activity.sqlite")
        self.assertEqual(count, 1)
        self.assertEqual(self.activity(1)[1], "2026-09-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
