// ============================================================
//  Hexagon Grid In A Polygon
//  Takes an arbitrary 2D polygon outline and fills it with a
//  hexagon (honeycomb) grid cut as negative space out of a
//  solid sheet of the polygon's shape.
// ============================================================

/* [Sheet outline] */
// Outline polygon as a list of [x, y] points (CCW). Edit freely.
sheet_poly = [
    [0,   0],
    [120, 12],
    [120, 60],
    [70,  100],
    [0,   90],
];
// Sheet height / thickness - Z (mm)
sheet_h  = 3;

/* [Hexagon] */
// Width of a hexagon hole across the flats (mm)
hex_size = 5;
// Wall / web thickness left between neighbouring holes (mm)
hex_wall = .75;

/* [Sections] */
// Max size of a single section (mm). The sheet is split into an
// even grid of pieces, none larger than this in X or Y.
section_max = 50;
// Gap between pieces in the exploded preview (mm)
section_gap = 5;
// Show a single piece for STL export: column index, 0-based (-1 = all)
section_i   = -1;
// Show a single piece for STL export: row index, 0-based (-1 = all)
section_j   = -1;
// Cut style:
//   "midpoint" - cut crosses the webs at their midpoints and turns
//                inside the hole centres, never through a junction
//   "web"      - cut runs along the web centre-lines (through junctions)
cut_style   = "midpoint";

/* [Connector] */
// Half-lap seam: along each shared edge one piece keeps the bottom
// half of the thickness and its neighbour keeps the top half, so the
// pieces overlap and interlock (each provides a different side).
lap_joint = true;
// Width of the overlap band straddling the seam (mm)
lap_width = 2;

// Resolution
$fn = 64;

// ------------------------------------------------------------
//  Derived geometry (pointy-top hexagons)
// ------------------------------------------------------------
hex_r    = hex_size / sqrt(3);       // centre -> vertex (circumradius)
col_step = hex_size + hex_wall;      // horizontal spacing
row_step = (hex_r * 1.5) + hex_wall; // vertical spacing
hex_cell = hex_size - hex_wall;      // across-flats of each hole

// bounding box of the polygon
poly_xs = [for (p = sheet_poly) p[0]];
poly_ys = [for (p = sheet_poly) p[1]];
min_x = min(poly_xs);  max_x = max(poly_xs);
min_y = min(poly_ys);  max_y = max(poly_ys);

// Section grid.
// Each hexagon cell is assigned to one section, and the cut runs
// along the webs between cells (the honeycomb pattern) so no
// hexagon is ever sliced. A cell's "territory" is its Voronoi
// region in the hex lattice, which is itself a hexagon.
span_x = max_x - min_x;
span_y = max_y - min_y;

// enough divisions that, even with the jagged honeycomb edge,
// no piece's footprint exceeds section_max
n_sx  = max(1, ceil(span_x / (section_max - col_step)));
n_sy  = max(1, ceil(span_y / (section_max - row_step)));
sec_w = span_x / n_sx;
sec_h = span_y / n_sy;

// Voronoi cell of the lattice (a hexagon): vertical web-centre
// edges at +/- col_step/2, sloped edges toward the row neighbours
vt_top  = (pow(col_step / 2, 2) + pow(row_step, 2)) / (2 * row_step);
vt_side = (pow(row_step, 2) - pow(col_step / 2, 2)) / (2 * row_step);

// ------------------------------------------------------------
//  Modules
// ------------------------------------------------------------

// A single pointy-top hexagon prism, sized by across-flats `af`
module hexagon(af, h) {
    r = af / sqrt(3);
    linear_extrude(height = h)
        rotate(30)
            circle(r = r, $fn = 6);
}

// Every hole across the polygon's bounding box (clipped by the
// sheet body, so border holes are simply cut to the outline)
module hex_holes() {
    n_cols = ceil((max_x - min_x) / col_step) + 2;
    n_rows = ceil((max_y - min_y) / row_step) + 2;
    for (row = [0 : n_rows]) {
        offset_x = (row % 2 == 0) ? 0 : col_step / 2;
        for (col = [0 : n_cols]) {
            c = [min_x + col * col_step + offset_x,
                 min_y + row * row_step];
            translate([c[0], c[1], -0.1])
                hexagon(hex_cell, sheet_h + 0.2);
        }
    }
}

// Solid sheet shaped like the polygon, with the hex grid removed
module sheet() {
    difference() {
        linear_extrude(height = sheet_h)
            polygon(points = sheet_poly);
        hex_holes();
    }
}

// The Voronoi tile (hexagon) owned by one lattice cell. Adjacent
// tiles meet exactly on the web centre-lines and tile with no gaps.
// Used by cut_style "web".
module cell_tile_2d() {
    polygon([[ col_step / 2,  vt_side],
             [ col_step / 2, -vt_side],
             [ 0,           -vt_top ],
             [-col_step / 2, -vt_side],
             [-col_step / 2,  vt_side],
             [ 0,            vt_top ]]);
}
module cell_tile(h) {
    linear_extrude(height = h) cell_tile_2d();
}

// A triangle prism between three hole centres. Used by cut_style
// "midpoint": triangle edges connect centres, so each edge crosses
// exactly one web at its midpoint and the corners sit in hole centres.
module tri(a, b, c, h) {
    linear_extrude(height = h)
        polygon([a, b, c]);
}

// Which section a point belongs to (its band in the even grid)
function sec_of(x, y) =
    [min(n_sx - 1, max(0, floor((x - min_x) / sec_w))),
     min(n_sy - 1, max(0, floor((y - min_y) / sec_h)))];

// Section a triangle belongs to, decided by its centroid
function tri_sec(a, b, c) =
    sec_of((a[0] + b[0] + c[0]) / 3, (a[1] + b[1] + c[1]) / 3);

// Clip mask for section (ti, tj): the union of all the tiles whose
// territory falls in that section's band.
module section_mask(ti, tj) {
    n_cols = ceil(span_x / col_step) + 2;
    n_rows = ceil(span_y / row_step) + 2;
    for (row = [-1 : n_rows + 1]) {
        offset_x = (row % 2 == 0) ? 0 : col_step / 2;
        for (col = [-1 : n_cols + 1]) {
            cx = min_x + col * col_step + offset_x;
            cy = min_y + row * row_step;

            if (cut_style == "web") {
                if (sec_of(cx, cy) == [ti, tj])
                    translate([cx, cy, -0.1])
                        cell_tile(sheet_h + 0.2);
            } else {
                // two Delaunay triangles fanning up from this centre
                p   = [cx,                  cy];
                e   = [cx + col_step,       cy];
                ne  = [cx + col_step / 2,   cy + row_step];
                nee = [cx + 3 * col_step / 2, cy + row_step];
                if (tri_sec(p, e, ne) == [ti, tj])
                    translate([0, 0, -0.1]) tri(p, e, ne, sheet_h + 0.2);
                if (tri_sec(e, nee, ne) == [ti, tj])
                    translate([0, 0, -0.1]) tri(e, nee, ne, sheet_h + 0.2);
            }
        }
    }
}

// 2D tiles belonging to section (ti, tj) - or, when `complement` is
// true, every tile that does NOT - each grown outward by `grow`.
// The loop is limited to the section's band (+margin) so it stays fast.
module sec_tiles_2d(ti, tj, grow, complement) {
    mgn    = lap_width + 2 * col_step;
    col_lo = floor((ti * sec_w - mgn) / col_step) - 1;
    col_hi = ceil(((ti + 1) * sec_w + mgn) / col_step) + 1;
    row_lo = floor((tj * sec_h - mgn) / row_step) - 1;
    row_hi = ceil(((tj + 1) * sec_h + mgn) / row_step) + 1;
    for (row = [row_lo : row_hi]) {
        offset_x = (row % 2 == 0) ? 0 : col_step / 2;
        for (col = [col_lo : col_hi]) {
            cx = min_x + col * col_step + offset_x;
            cy = min_y + row * row_step;
            if (cut_style == "web") {
                if ((sec_of(cx, cy) == [ti, tj]) != complement)
                    translate([cx, cy]) offset(delta = grow) cell_tile_2d();
            } else {
                p   = [cx,                  cy];
                e   = [cx + col_step,       cy];
                ne  = [cx + col_step / 2,   cy + row_step];
                nee = [cx + 3 * col_step / 2, cy + row_step];
                if ((tri_sec(p, e, ne) == [ti, tj]) != complement)
                    offset(delta = grow) polygon([p, e, ne]);
                if ((tri_sec(e, nee, ne) == [ti, tj]) != complement)
                    offset(delta = grow) polygon([e, nee, ne]);
            }
        }
    }
}

// One section. With lap_joint off it is just the sheet clipped to its
// territory. With lap_joint on, the band straddling each seam is
// reduced to half thickness on the section's parity-chosen Z-side, so
// neighbouring pieces overlap and interlock.
module section(i, j) {
    if (!lap_joint) {
        intersection() {
            sheet();
            section_mask(i, j);
        }
    } else {
        // bottom half on even parity, top half on odd parity, so any
        // two edge-sharing sections take opposite halves of the seam
        z0 = ((i + j) % 2 == 0) ? 0 : sheet_h / 2;
        intersection() {
            sheet();
            union() {
                // full-thickness core: territory minus the seam band
                linear_extrude(height = sheet_h)
                    difference() {
                        sec_tiles_2d(i, j, 0, false);
                        sec_tiles_2d(i, j, lap_width / 2, true);
                    }
                // half-thickness overlap across the seam band
                translate([0, 0, z0])
                    linear_extrude(height = sheet_h / 2)
                        intersection() {
                            sec_tiles_2d(i, j, lap_width / 2, false);
                            sec_tiles_2d(i, j, lap_width / 2, true);
                        }
            }
        }
    }
}

// Lay every section out with a gap between pieces (preview),
// or render a single piece when section_i / section_j are set.
module sections() {
    if (section_i >= 0 && section_j >= 0) {
        section(section_i, section_j);
    } else {
        for (i = [0 : n_sx - 1])
            for (j = [0 : n_sy - 1])
                translate([i * (sec_w + section_gap),
                           j * (sec_h + section_gap), 0])
                    section(i, j);
    }
}

sections();
