//! Terrain drawing. Each region's ground is one sprite from its shape mask
//! (`RegionView::rows`), so places look like patches of land that fit into each other.
//! Walls, fences, water and doorways (`WorldView::tiles`) are one more sprite on top.

use bevy::asset::RenderAssetUsages;
use bevy::image::ImageSampler;
use bevy::prelude::*;
use bevy::render::render_resource::{Extent3d, TextureDimension, TextureFormat};
use std::collections::HashMap;

use crate::net::{RegionView, WorldSnapshot};

/// Texture pixels per world cell (the edge darkening is one pixel wide).
const PX_PER_CELL: usize = 4;
/// Open space between regions (wilderness).
pub const WILDERNESS: Color = Color::srgb(0.07, 0.10, 0.09);
/// Texture pixels per world cell for walls / fences (enough for a brick pattern).
const STRUCT_PX: usize = 8;

/// One texture per region shape, rebuilt only when a shape changes.
#[derive(Resource, Default)]
pub struct RegionImages {
    images: HashMap<String, (Vec<String>, Handle<Image>)>,
}

impl RegionImages {
    pub fn get(&self, region: &str) -> Option<Handle<Image>> {
        self.images.get(region).map(|(_, h)| h.clone())
    }
}

/// White ground with a darker rim (tinted per region by the sprite colour); clear outside.
pub fn region_image(rows: &[String]) -> Image {
    let h = rows.len().max(1);
    let w = rows.iter().map(|r| r.chars().count()).max().unwrap_or(1).max(1);
    let grid: Vec<Vec<bool>> = rows.iter().map(|r| r.chars().map(|c| c == '#').collect()).collect();
    let at = |x: isize, y: isize| -> bool {
        x >= 0 && y >= 0 && grid.get(y as usize).and_then(|r| r.get(x as usize)).copied().unwrap_or(false)
    };
    let (pw, ph) = (w * PX_PER_CELL, h * PX_PER_CELL);
    let mut data = vec![0u8; pw * ph * 4];
    for py in 0..ph {
        for px in 0..pw {
            let (cx, cy) = ((px / PX_PER_CELL) as isize, (py / PX_PER_CELL) as isize);
            if !at(cx, cy) {
                continue;
            }
            let (sx, sy) = (px % PX_PER_CELL, py % PX_PER_CELL);
            let last = PX_PER_CELL - 1;
            let rim = (sx == 0 && !at(cx - 1, cy))
                || (sx == last && !at(cx + 1, cy))
                || (sy == 0 && !at(cx, cy - 1))
                || (sy == last && !at(cx, cy + 1));
            let shade = if rim { 150 } else { 255 };
            let i = (py * pw + px) * 4;
            data[i..i + 4].copy_from_slice(&[shade, shade, shade, 255]);
        }
    }
    let mut image = Image::new(
        Extent3d { width: pw as u32, height: ph as u32, depth_or_array_layers: 1 },
        TextureDimension::D2,
        data,
        TextureFormat::Rgba8UnormSrgb,
        RenderAssetUsages::RENDER_WORLD,
    );
    image.sampler = ImageSampler::nearest();
    image
}

/// Walls, fences, water and doorways over the whole world; everything else clear.
pub fn structure_image(tiles: &[String]) -> Image {
    let h = tiles.len().max(1);
    let w = tiles.iter().map(|r| r.len()).max().unwrap_or(1).max(1);
    let at = |x: isize, y: isize| -> u8 {
        if x < 0 || y < 0 {
            return b' ';
        }
        tiles.get(y as usize).and_then(|r| r.as_bytes().get(x as usize)).copied().unwrap_or(b' ')
    };
    let (pw, ph) = (w * STRUCT_PX, h * STRUCT_PX);
    let mut data = vec![0u8; pw * ph * 4];
    let last = STRUCT_PX - 1;
    for cy in 0..h {
        for cx in 0..w {
            let kind = at(cx as isize, cy as isize);
            if !matches!(kind, b'#' | b'%' | b'~' | b'+' | b'^') {
                continue;
            }
            let (x, y) = (cx as isize, cy as isize);
            for sy in 0..STRUCT_PX {
                for sx in 0..STRUCT_PX {
                    let edge = |dx: isize, dy: isize| at(x + dx, y + dy) != kind;
                    let rim = (sx == 0 && edge(-1, 0))
                        || (sx == last && edge(1, 0))
                        || (sy == 0 && edge(0, -1))
                        || (sy == last && edge(0, 1));
                    let rgba: [u8; 4] = match kind {
                        // Stone wall: courses of bricks, darker mortar, dark outline.
                        b'#' => {
                            let row = (cy * STRUCT_PX + sy) / 4;
                            let offset = if row % 2 == 0 { 0 } else { 4 };
                            let mortar = sy % 4 == 3 || (cx * STRUCT_PX + sx + offset) % 8 == 0;
                            if rim {
                                [46, 42, 40, 255]
                            } else if mortar {
                                [92, 86, 80, 255]
                            } else {
                                [138, 128, 116, 255]
                            }
                        }
                        // Hedge / fence: dark leaves with lighter flecks.
                        b'%' => {
                            let fleck = (sx * 7 + sy * 13 + cx * 3 + cy * 5) % 9 == 0;
                            if rim {
                                [22, 44, 22, 255]
                            } else if fleck {
                                [74, 120, 58, 255]
                            } else {
                                [42, 78, 38, 255]
                            }
                        }
                        // Tree: a round canopy (lighter top-left), trunk shadow showing at the bottom.
                        b'^' => {
                            let (fx, fy) = (sx as f32 - 3.5, sy as f32 - 3.2);
                            let r2 = fx * fx + fy * fy;
                            if r2 <= 12.5 {
                                if fx + fy < -3.0 { [92, 150, 70, 255] } else if r2 > 9.0 { [30, 70, 34, 255] } else { [52, 108, 50, 255] }
                            } else if sy >= 6 && (3..=4).contains(&sx) {
                                [84, 58, 36, 255]
                            } else {
                                [0, 0, 0, 0]
                            }
                        }
                        // Water: blue with a few wave highlights.
                        b'~' => {
                            let wave = (sx + cx * 3 + (sy / 3) * 5) % 8 == 0 && sy % 3 == 1;
                            if wave {
                                [120, 170, 210, 255]
                            } else {
                                [38, 78, 124, 255]
                            }
                        }
                        // Doorway: a wooden threshold in the gap.
                        _ => {
                            let plank = sx % 3 == 2 || sy % 3 == 2;
                            if plank {
                                [96, 64, 34, 200]
                            } else {
                                [128, 88, 48, 200]
                            }
                        }
                    };
                    let i = ((cy * STRUCT_PX + sy) * pw + cx * STRUCT_PX + sx) * 4;
                    data[i..i + 4].copy_from_slice(&rgba);
                }
            }
        }
    }
    let mut image = Image::new(
        Extent3d { width: pw as u32, height: ph as u32, depth_or_array_layers: 1 },
        TextureDimension::D2,
        data,
        TextureFormat::Rgba8UnormSrgb,
        RenderAssetUsages::RENDER_WORLD,
    );
    image.sampler = ImageSampler::nearest();
    image
}

/// The walls / fences texture, rebuilt only when the terrain changes.
#[derive(Resource, Default)]
pub struct StructureImage {
    tiles: Vec<String>,
    handle: Option<Handle<Image>>,
}

impl StructureImage {
    pub fn get(&self) -> Option<Handle<Image>> {
        self.handle.clone()
    }
}

pub fn sync_structure_image(world: Res<WorldSnapshot>, mut cache: ResMut<StructureImage>, mut assets: ResMut<Assets<Image>>) {
    if !world.is_changed() || cache.tiles == world.world.tiles {
        return;
    }
    if let Some(old) = cache.handle.take() {
        assets.remove(old.id());
    }
    cache.tiles = world.world.tiles.clone();
    if !cache.tiles.is_empty() {
        cache.handle = Some(assets.add(structure_image(&cache.tiles)));
    }
}

/// Keep one texture per region in step with the server's shapes.
pub fn sync_region_images(world: Res<WorldSnapshot>, mut cache: ResMut<RegionImages>, mut assets: ResMut<Assets<Image>>) {
    if !world.is_changed() {
        return;
    }
    let regions: &[RegionView] = &world.world.regions;
    cache.images.retain(|name, (_, handle)| {
        let keep = regions.iter().any(|r| r.name == *name && !r.rows.is_empty());
        if !keep {
            assets.remove(handle.id());
        }
        keep
    });
    for region in regions.iter().filter(|r| !r.rows.is_empty()) {
        if cache.images.get(&region.name).is_some_and(|(rows, _)| *rows == region.rows) {
            continue;
        }
        let handle = assets.add(region_image(&region.rows));
        if let Some((_, old)) = cache.images.insert(region.name.clone(), (region.rows.clone(), handle)) {
            assets.remove(old.id());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ground_has_a_rim_and_holes_stay_clear() {
        let rows = vec!["##".to_string(), "#.".to_string()];
        let image = region_image(&rows);
        let data = image.data.as_ref().unwrap();
        let w = 2 * PX_PER_CELL;
        let px = |x: usize, y: usize| &data[(y * w + x) * 4..(y * w + x) * 4 + 4];
        assert_eq!(px(1, 1), &[255, 255, 255, 255], "inside a cell");
        assert_eq!(px(0, 1)[0], 150, "edge of the land is darker");
        assert_eq!(px(w - 1, w - 1)[3], 0, "outside the shape is clear");
    }

    #[test]
    fn walls_are_drawn_and_ground_stays_clear() {
        let tiles = vec!["#.".to_string(), "+%".to_string()];
        let image = structure_image(&tiles);
        let data = image.data.as_ref().unwrap();
        let w = 2 * STRUCT_PX;
        let px = |x: usize, y: usize| &data[(y * w + x) * 4..(y * w + x) * 4 + 4];
        assert_eq!(px(1, 1)[3], 255, "wall is opaque");
        assert_eq!(px(STRUCT_PX + 2, 2)[3], 0, "ground cell is clear");
        assert!(px(2, STRUCT_PX + 2)[3] > 0, "doorway has a threshold");
        assert!(px(STRUCT_PX + 3, STRUCT_PX + 3)[1] > px(STRUCT_PX + 3, STRUCT_PX + 3)[0], "hedge is green");
    }
}
