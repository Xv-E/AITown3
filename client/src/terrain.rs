//! Region ground: each region is drawn from its shape mask (`RegionView::rows`) as one
//! sprite, so places look like rounded patches of land instead of rectangles.

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
}
