import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from photos.cluster import (
    haversine_km, time_gap_split, location_spread_km,
    detect_home, is_home_cluster, build_clusters
)

def _p(id, taken_at, lat=None, lon=None, place=None):
    return {"id": id, "taken_at": taken_at, "lat": lat, "lon": lon, "place": place}

DAY = 86400

def test_haversine_london_paris():
    d = haversine_km(51.5, -0.1, 48.8, 2.3)
    assert 335 < d < 350

def test_haversine_same_point():
    assert haversine_km(10.0, 20.0, 10.0, 20.0) == 0.0

def test_time_gap_split_one_group():
    photos = [_p(1, 1000000), _p(2, 1000100)]
    groups = time_gap_split(photos, gap_seconds=3 * DAY)
    assert len(groups) == 1
    assert len(groups[0]) == 2

def test_time_gap_split_two_groups():
    photos = [_p(1, 1000000), _p(2, 1000000 + 4 * DAY)]
    groups = time_gap_split(photos, gap_seconds=3 * DAY)
    assert len(groups) == 2

def test_location_spread_no_gps():
    photos = [_p(1, 1000), _p(2, 2000)]
    assert location_spread_km(photos) == 0.0

def test_location_spread_nearby():
    photos = [
        _p(1, 1000, lat=37.7, lon=-122.4),
        _p(2, 2000, lat=37.8, lon=-122.5),
    ]
    assert location_spread_km(photos) < 20

def test_detect_home_no_gps():
    assert detect_home([_p(1, 1000)]) is None

def test_detect_home_most_common():
    photos = [
        _p(1, 1000, lat=37.7, lon=-122.4),
        _p(2, 2000, lat=37.7, lon=-122.4),
        _p(3, 3000, lat=37.7, lon=-122.4),
        _p(4, 4000, lat=64.1, lon=-21.9),
    ]
    home = detect_home(photos)
    assert home is not None
    assert abs(home[0] - 37.5) < 1.0  # SF cell

def test_is_home_cluster_near():
    home = (37.7, -122.4)
    photos = [_p(1, 1000, lat=37.7, lon=-122.4), _p(2, 2000, lat=37.8, lon=-122.5)]
    assert is_home_cluster(photos, home) is True

def test_is_home_cluster_away():
    home = (37.7, -122.4)
    photos = [_p(1, 1000, lat=64.1, lon=-21.9)]
    assert is_home_cluster(photos, home) is False

def test_is_home_cluster_no_gps_unknown():
    assert is_home_cluster([_p(1, 1000)], home=(37.7, -122.4)) is False

def test_build_clusters_empty():
    assert build_clusters([]) == []

def test_build_clusters_monthly_catchall():
    base = 1704067200  # 2024-01-01
    photos = [
        _p(1, base,         lat=37.7, lon=-122.4, place="San Francisco, US"),
        _p(2, base + DAY,   lat=37.7, lon=-122.4, place="San Francisco, US"),
    ]
    clusters = build_clusters(photos, gap_days=3, radius_km=50)
    assert len(clusters) >= 1
    c = clusters[0]
    assert c["is_trip"] is False
    assert c["confirmed"] is True
    assert c["name"].startswith("2024-01")

def test_build_clusters_trip_detected():
    base = 1704067200  # 2024-01-01
    photos = []
    for i in range(5):
        photos.append(_p(i+1, base + i*DAY, lat=37.7, lon=-122.4, place="San Francisco, US"))
    for i in range(5):
        photos.append(_p(i+6, base + (15+i)*DAY, lat=64.1, lon=-21.9, place="Reykjavik, Iceland"))

    clusters = build_clusters(photos, gap_days=3, radius_km=50)
    trips = [c for c in clusters if c["is_trip"]]
    assert len(trips) == 1
    assert "Iceland" in (trips[0]["place"] or "")
    assert trips[0]["confirmed"] is False

def test_build_clusters_undated_goes_to_no_date():
    photos = [_p(1, None)]
    clusters = build_clusters(photos)
    assert any(c["name"] == "no-date" for c in clusters)

def test_build_clusters_photo_ids_correct():
    base = 1704067200
    photos = [_p(1, base), _p(2, base + DAY)]
    clusters = build_clusters(photos)
    assert clusters[0]["photo_ids"] == [1, 2]
