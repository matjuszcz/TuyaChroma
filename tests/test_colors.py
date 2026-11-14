from tuyachroma.colors import adjust, distance, reduce_colors, zone_indices


def test_zones_keyboard() -> None:
    left = zone_indices("keyboard", "left")
    right = zone_indices("keyboard", "right")
    assert len(left) == 6 * 7 and len(right) == 6 * 7
    assert 0 in left and 21 in right and 131 in right
    assert len(zone_indices("keyboard", "all")) == 132
    assert zone_indices("chromalink", 2) == [2]
    assert zone_indices("chromalink", [1, 3]) == [1, 3]
    assert zone_indices("chromalink", [99]) == [0, 1, 2, 3, 4]


def test_vivid_prefers_colored_keys() -> None:
    colors = [(10, 10, 10)] * 120 + [(255, 0, 0)] * 12
    r, g, b = reduce_colors(colors, "vivid")
    assert r > 200 and g < 60 and b < 60
    avg = reduce_colors(colors, "average")
    assert avg[0] < 40  # plain average is dark and greyish


def test_reduce_black() -> None:
    assert reduce_colors([(0, 0, 0)] * 5) == (0, 0, 0)
    assert reduce_colors([]) == (0, 0, 0)


def test_adjust() -> None:
    assert adjust((255, 0, 0), 0.5) == (128, 0, 0)
    assert adjust((0, 0, 0), 1.0, 0.2) == (0, 0, 0)
    dim = adjust((10, 0, 0), 1.0, 0.5)
    assert dim[0] >= 128
    assert distance((1, 2, 3), (4, 2, 0)) == 3
