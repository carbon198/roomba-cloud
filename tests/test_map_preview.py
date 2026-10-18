"""Synthetic geometry, coordinate alignment, resource bounds and private PNG output."""

import contextlib
import hashlib
import io
import os
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zlib

from PIL import Image

from roomba_cloud.maps.preview import (
    BACKGROUND,
    PALETTE,
    decode_geometry,
    display_raster,
    main,
    render_png,
)
from roomba_cloud.maps.catalog import archive_maps
from test_room_catalog import binary_rooms, field


def float_field(number, value):
    return bytes([(number << 3) | 5]) + struct.pack("<f", value)


def geometry(
    *,
    width=12,
    height=10,
    resolution=0.05,
    raster=None,
    contours=None,
    dock=True,
    head_extra=b"",
    structure=b"",
    adjacency=None,
):
    head = (
        field(2, width)
        + field(3, height)
        + float_field(4, -1)
        + float_field(5, -2)
        + float_field(6, -1 + width * resolution)
        + float_field(7, -2 + height * resolution)
        + float_field(8, resolution)
        + head_extra
    )
    if raster is None:
        raster = bytes(1 if x < 6 else 2 for y in range(height) for x in range(width))
    if contours is None:
        contours = [
            (1, [(0, 0), (5, 0), (5, 9), (0, 9)]),
            (2, [(6, 0), (11, 0), (11, 9), (6, 9)]),
        ]
    data = (
        field(3, head)
        + field(4, field(1, raster))
        + zlib.decompress(binary_rooms([(1, "West"), (2, "East")]))
    )
    for identifier, points in contours:
        chain = field(1, identifier)
        for x, y in points:
            chain += field(2, field(1, x) + field(2, y))
        data += field(14, chain)
    if dock:
        data += field(7, float_field(1, -0.9) + float_field(2, -1.85) + float_field(3, 0))
    if structure:
        data += field(19, structure)
    if adjacency is not None:
        data += field(13, field(1, adjacency))
    return zlib.compress(data)


class MapPreviewTests(unittest.TestCase):
    def test_out_of_bounds_dock_is_unavailable_without_losing_geometry(self):
        expected = decode_geometry(geometry(dock=False))
        for x, y in ((1100, 1100), (-1100, -1100), (-0.9, 1100), (1100, -1.85)):
            with self.subTest(x=x, y=y):
                data = zlib.decompress(geometry(dock=False))
                data += field(7, float_field(1, x) + float_field(2, y))
                model = decode_geometry(zlib.compress(data))
                self.assertEqual(model, expected)
                self.assertEqual(render_png(model), render_png(expected))

    def test_malformed_dock_coordinates_still_fail(self):
        for pose in (
            float_field(1, float("nan")) + float_field(2, 1100),
            float_field(1, 1100) + float_field(2, float("inf")),
            float_field(1, 1100),
        ):
            with self.subTest(pose=pose):
                data = zlib.decompress(geometry(dock=False)) + field(7, pose)
                with self.assertRaises(ValueError):
                    decode_geometry(zlib.compress(data))

    def test_post_cleaning_unassigned_structural_walls_do_not_create_a_room(self):
        line = field(2, float_field(1, -0.85) + float_field(2, -1.6)) + field(
            3, float_field(1, -0.55) + float_field(2, -1.6)
        )
        structure = field(1, field(1, 1) + field(2, line))
        structure += field(1, field(1, 0) + field(2, line))
        model = decode_geometry(geometry(structure=structure))
        self.assertEqual(len(model.structural_lines), 2)
        self.assertEqual(set(model.rooms), {"1", "2"})
        self.assertEqual(set(model.contours), {"1", "2"})
        self.assertTrue(render_png(model).startswith(b"\x89PNG\r\n\x1a\n"))
        for invalid in (
            field(1, field(2, line)),
            field(1, field(1, 0) + field(1, 0) + field(2, line)),
            field(1, field(1, 0) + field(2, field(2, b""))),
            field(1, field(1, 99) + field(2, line)),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                decode_geometry(geometry(structure=invalid))

    def test_room_fill_edges_interpolate_across_grid_cells_without_blurring_interior(self):
        from dataclasses import replace

        model = replace(
            decode_geometry(geometry(width=30, height=30, dock=False)),
            raster=bytes(900),
            contours={
                "1": ((8, 8), (21, 8), (21, 21), (8, 21)),
                "2": ((10, 23), (12, 23), (12, 25), (10, 25)),
            },
            structural_lines=(((8, 21), (21, 21)),),
        )
        original = model.raster
        with patch("PIL.Image.blend", wraps=Image.blend) as blend:
            rendered = render_png(model, size=1000)
        blend.assert_called_once()
        crisp, smooth, amount = blend.call_args.args
        self.assertEqual(amount, 0.5)
        self.assertNotEqual(crisp.tobytes(), smooth.tobytes())
        with Image.open(io.BytesIO(rendered)) as image:
            # At this scale one grid cell is ~27 pixels: smoothing should span
            # that cell, rather than just the final downsampling fringe.
            transition = [image.getpixel((x, 500)) for x in range(270, 340)]
            self.assertGreater(
                len({color for color in transition if color not in (BACKGROUND, PALETTE[0])}), 10
            )
            self.assertEqual(image.getpixel((200, 500)), BACKGROUND)
            self.assertEqual(image.getpixel((500, 500)), PALETTE[0])
        self.assertEqual(model.raster, original)

    def test_navigable_contour_strokes_are_omitted_when_structural_walls_exist(self):
        from dataclasses import replace

        model = replace(
            decode_geometry(geometry(dock=False)), structural_lines=(((0, 0), (11, 9)),)
        )
        changed = replace(
            model,
            contours={
                "1": ((1, 1), (4, 1), (4, 8), (1, 8)),
                "2": ((7, 1), (10, 1), (10, 8), (7, 8)),
            },
        )
        for outlines in (False, True):
            self.assertEqual(
                render_png(model, size=256, outlines_only=outlines),
                render_png(changed, size=256, outlines_only=outlines),
            )

    def test_solid_room_fills_preserve_exterior_known_rooms_and_ambiguous_overlaps(self):
        from dataclasses import replace

        model = decode_geometry(geometry(width=30, height=30, dock=False))
        raster = bytearray(900)
        raster[5 * 30 + 4] = 200
        raster[6 * 30 + 4] = 2
        model = replace(
            model,
            raster=bytes(raster),
            contours={
                "1": ((2, 2), (15, 2), (15, 20), (2, 20)),
                "2": ((12, 2), (25, 2), (25, 20), (12, 20)),
            },
        )
        cleaned = display_raster(model)
        self.assertEqual(cleaned[5 * 30 + 4], 1)
        self.assertEqual(cleaned[6 * 30 + 4], 2)
        self.assertEqual(cleaned[10 * 30 + 8], 1)
        self.assertEqual(cleaned[10 * 30 + 22], 2)
        self.assertEqual(cleaned[10 * 30 + 13], 0)
        self.assertEqual(cleaned[0], 0)
        self.assertEqual(cleaned[29 * 30 + 8], 0)
        self.assertEqual(model.raster, bytes(raster))
        self.assertEqual(display_raster(replace(model, resolution=0.2)), cleaned)

    def test_antialiases_diagonal_edges_without_blurring_room_interiors(self):
        from dataclasses import replace

        model = decode_geometry(geometry(dock=False))
        model = replace(model, structural_lines=(((1, 1), (9, 8)),))
        with Image.open(io.BytesIO(render_png(model, size=256, outlines_only=True))) as image:
            colors = {color for _, color in image.getcolors(image.width * image.height)}
            self.assertIn(BACKGROUND, colors)
            self.assertTrue(any(BACKGROUND[0] < r < 180 and r == g == b for r, g, b in colors))
            self.assertEqual(image.size, (256, 256))

    def test_explicit_room_adjacency_and_invalid_matrices(self):
        self.assertIsNone(decode_geometry(geometry()).adjacent_rooms)
        self.assertEqual(
            decode_geometry(geometry(adjacency=b"\0\7\7\0")).adjacent_rooms,
            {"1": ("2",), "2": ("1",)},
        )
        self.assertEqual(
            decode_geometry(geometry(adjacency=b"\0" * 4)).adjacent_rooms, {"1": (), "2": ()}
        )
        for matrix in (b"", b"\0\1", b"\1\1\1\0", b"\0\1\0\0"):
            with self.subTest(matrix=matrix), self.assertRaisesRegex(ValueError, "adjacency"):
                decode_geometry(geometry(adjacency=matrix))

    def test_structural_lines_use_world_coordinates_and_are_drawn(self):
        line = (
            field(1, 0)
            + field(2, float_field(1, -0.85) + float_field(2, -1.6))
            + field(3, float_field(1, -0.55) + float_field(2, -1.6))
        )
        structure = field(1, field(1, 1) + field(2, line))
        model = decode_geometry(geometry(structure=structure))
        self.assertEqual(len(model.structural_lines), 1)
        for actual, expected in zip(model.structural_lines[0], ((2.5, 7.5), (8.5, 7.5))):
            self.assertAlmostEqual(actual[0], expected[0], places=4)
            self.assertAlmostEqual(actual[1], expected[1], places=4)
        for outlines in (False, True):
            with Image.open(
                io.BytesIO(render_png(model, size=256, outlines_only=outlines))
            ) as image:
                self.assertLess(
                    min(
                        max(abs(component - 180) for component in image.getpixel((128, y)))
                        for y in range(80, 90)
                    ),
                    10,
                )
        invalid = [
            field(1, field(1, 99) + field(2, line)),
            field(1, field(1, 1) + field(2, field(2, b""))),
            field(1, field(1, 1) + field(2, line + field(2, b""))),
            field(
                1,
                field(1, 1)
                + field(
                    2,
                    field(2, float_field(1, 100) + float_field(2, -1.6))
                    + field(3, float_field(1, -0.55) + float_field(2, -1.6)),
                ),
            ),
        ]
        for structure in invalid:
            with self.assertRaises(ValueError):
                decode_geometry(geometry(structure=structure))
        self.assertFalse(decode_geometry(geometry()).structural_lines)

    def test_exact_geometry_and_world_to_grid_dock(self):
        model = decode_geometry(geometry())
        self.assertEqual((model.width, model.height), (12, 10))
        self.assertAlmostEqual(model.resolution, 0.05)
        self.assertEqual(model.rooms, {"1": "West", "2": "East"})
        self.assertEqual(model.contours["1"], ((0, 0), (5, 0), (5, 9), (0, 9)))
        self.assertAlmostEqual(model.dock[0], 2, places=4)
        self.assertAlmostEqual(model.dock[1], 3, places=4)
        self.assertIsNone(decode_geometry(geometry(dock=False)).dock)

    def test_png_size_axis_direction_floor_palette_and_dock(self):
        model = decode_geometry(geometry())
        with Image.open(io.BytesIO(render_png(model, size=256))) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (256, 256))
            image = image.convert("RGB")
            self.assertEqual(image.getpixel((65, 83)), PALETTE[0])
            self.assertEqual(image.getpixel((173, 83)), PALETTE[1])
            self.assertEqual(image.getpixel((74, 151)), (204, 211, 87))
            self.assertEqual(image.getpixel((5, 5)), BACKGROUND)
        with Image.open(io.BytesIO(render_png(model, size=256, outlines_only=True))) as image:
            self.assertEqual(image.getpixel((65, 83)), BACKGROUND)
            self.assertEqual(image.getpixel((74, 151)), (204, 211, 87))
        # Unknown cells inside a room contour use that room's solid display fill.
        raster = bytes([200]) * 120
        with Image.open(
            io.BytesIO(render_png(decode_geometry(geometry(raster=raster)), size=256))
        ) as image:
            self.assertEqual(image.getpixel((65, 83)), PALETTE[0])

    def test_zoomed_out_map_has_centered_margin_at_all_output_sizes(self):
        from PIL import ImageChops

        model = decode_geometry(geometry(dock=False))
        for size in (128, 256, 1000, 2048):
            for outlines in (False, True):
                with Image.open(
                    io.BytesIO(render_png(model, size=size, outlines_only=outlines))
                ) as image:
                    background = Image.new("RGB", image.size, BACKGROUND)
                    left, top, right, bottom = ImageChops.difference(image, background).getbbox()
                    expected_width = round((size - 40) * 0.85)
                    # Lanczos antialiasing extends a faint fringe by up to 3 pixels.
                    self.assertLessEqual(right - left, expected_width + 6)
                    self.assertAlmostEqual((left + right) / 2, size / 2, delta=2)
                    self.assertAlmostEqual((top + bottom) / 2, size / 2, delta=2)
                    self.assertGreaterEqual(left, (size - expected_width) // 2 - 3)

    def test_reject_malformed_dimensions_bounds_points_and_room_references(self):
        cases = [
            geometry(raster=b"short"),
            geometry(width=4097, raster=b""),
            geometry(width=0, raster=b""),
            geometry(resolution=0),
            geometry(resolution=float("nan")),
            geometry(head_extra=float_field(8, 0.05)),
            geometry(contours=[]),
            geometry(contours=[(1, [(0, 0), (1, 0), (20, 1)])]),
            geometry(contours=[(9, [(0, 0), (1, 0), (1, 1)])]),
            geometry(contours=[(1, [(0, 0), (1, 0)])]),
            geometry(contours=[(1, [(0, 0), (1, 0), (1, 1)])] * 2),
            zlib.compress(zlib.decompress(geometry()) + field(3, b"")),
            geometry() + b"trailing",
        ]
        for raw in cases:
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                decode_geometry(raw)
        with patch("roomba_cloud.maps.preview.MAX_POINTS", 3), self.assertRaises(ValueError):
            decode_geometry(geometry())
        with patch("roomba_cloud.maps.preview.MAX_CELLS", 100), self.assertRaises(ValueError):
            decode_geometry(geometry())
        for size in (127, 2049, True):
            with self.assertRaises(ValueError):
                render_png(decode_geometry(geometry()), size=size)

    def test_private_multi_map_cli_without_extraction_or_overwrite(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "saved.tar.gz"
            output = root / "previews"
            output.mkdir(mode=0o700)
            manifest = {
                "schema_version": 1,
                "files": [
                    {
                        "name": f"map-{index}.bin",
                        "type": "p2mapv",
                        "p2mapv": {"p2map_id": f"map-{index}", "p2mapv_id": "1.0", "p2map_type": 3},
                    }
                    for index in (1, 2)
                ],
            }
            with tarfile.open(archive_path, "w") as archive:
                for name, raw in [
                    ("manifest.json", json.dumps(manifest).encode()),
                    ("map-1.bin", geometry()),
                    ("map-2.bin", geometry()),
                ]:
                    info = tarfile.TarInfo(name)
                    info.size = len(raw)
                    archive.addfile(info, io.BytesIO(raw))
            before = archive_path.read_bytes()
            digest = hashlib.sha256(before).hexdigest()
            self.assertEqual(len(list(archive_maps(str(archive_path), len(before), digest))), 2)
            with contextlib.redirect_stdout(io.StringIO()):
                main([str(archive_path), "--output-dir", str(output)])
            files = sorted(output.iterdir())
            self.assertEqual(
                {f.name for f in files},
                {
                    f"saved-map-{index}-{style}.png"
                    for index in (1, 2)
                    for style in ("floor", "rooms")
                },
            )
            self.assertTrue(all(f.stat().st_mode & 0o777 == 0o600 for f in files))
            snapshots = {f: f.read_bytes() for f in files}
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main([str(archive_path), "--output-dir", str(output)])
            self.assertEqual({f: f.read_bytes() for f in files}, snapshots)
            self.assertEqual(before, archive_path.read_bytes())
            self.assertEqual(set(root.iterdir()), {archive_path, output})
            os.chmod(output, 0o755)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main([str(archive_path), "--output-dir", str(output)])


if __name__ == "__main__":
    unittest.main()
