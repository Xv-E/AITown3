//! World map overlay (M): the whole seamless world to scale — regions, who is where.
//! Click a region and the player walks there on their own (right-click / any move key stops it).

use bevy::{math::Isometry2d, prelude::*};
use std::collections::HashSet;

use crate::net::{RegionView, WorldSnapshot};
use crate::terrain::{RegionImages, StructureImage};
use crate::scene::WalkGrid;
use crate::ui::UiState;

const MAP_BOX: Vec2 = Vec2::new(900.0, 470.0);
const MAP_CENTER: Vec2 = Vec2::new(0.0, -30.0);
const MAP_Z: f32 = 40.0;
/// Walk to somewhere this close to the region's centre.
const ARRIVE_RADIUS: i32 = 3;

#[derive(Component)]
pub struct WorldMapEntity;

#[derive(Resource, Default)]
pub struct WorldMapState {
    key: Option<(u64, String)>,
}

/// World cell → overlay position (relative to the camera, which the overlay follows).
struct MapFrame {
    scale: f32,
    origin: Vec2,
}

impl MapFrame {
    fn of(world: &WorldSnapshot) -> Self {
        let size = Vec2::new(world.world.width.max(1) as f32, world.world.height.max(1) as f32);
        let scale = (MAP_BOX.x / size.x).min(MAP_BOX.y / size.y);
        Self {
            scale,
            origin: MAP_CENTER + Vec2::new(-size.x, size.y) * scale * 0.5,
        }
    }

    fn point(&self, x: f32, y: f32) -> Vec2 {
        self.origin + Vec2::new(x, -y) * self.scale
    }

    /// Overlay point → world cell.
    fn cell(&self, overlay: Vec2) -> IVec2 {
        let rel = (overlay - self.origin) / self.scale;
        IVec2::new(rel.x.floor() as i32, (-rel.y).floor() as i32)
    }

    fn region_rect(&self, r: &RegionView) -> (Vec2, Vec2) {
        let size = Vec2::new(r.w as f32, r.h as f32) * self.scale;
        (self.point(r.x as f32, r.y as f32) + Vec2::new(size.x, -size.y) * 0.5, size)
    }
}

fn sorted_names(world: &WorldSnapshot) -> Vec<String> {
    let mut names: Vec<String> = world.world.regions.iter().map(|r| r.name.clone()).collect();
    names.sort();
    names
}

pub fn map_input(
    keys: &ButtonInput<KeyCode>,
    world: &WorldSnapshot,
    grid: &WalkGrid,
    here: Option<IVec2>,
    ui: &mut UiState,
) {
    if keys.just_pressed(KeyCode::Escape) || keys.just_pressed(KeyCode::KeyM) {
        ui.world_map = None;
        return;
    }
    let names = sorted_names(world);
    let Some(view) = ui.world_map.as_mut() else {
        return;
    };
    if names.is_empty() {
        return;
    }
    let step: isize = if keys.just_pressed(KeyCode::KeyD)
        || keys.just_pressed(KeyCode::ArrowRight)
        || keys.just_pressed(KeyCode::KeyS)
        || keys.just_pressed(KeyCode::ArrowDown)
    {
        1
    } else if keys.just_pressed(KeyCode::KeyA)
        || keys.just_pressed(KeyCode::ArrowLeft)
        || keys.just_pressed(KeyCode::KeyW)
        || keys.just_pressed(KeyCode::ArrowUp)
    {
        -1
    } else {
        0
    };
    if step != 0 {
        let idx = names.iter().position(|n| *n == view.selected).unwrap_or(0) as isize;
        view.selected = names[(idx + step).rem_euclid(names.len() as isize) as usize].clone();
    }
    if keys.just_pressed(KeyCode::Enter) || keys.just_pressed(KeyCode::KeyE) {
        let target = view.selected.clone();
        walk_to_region(&target, world, grid, here, ui);
    }
}

/// The region under an overlay point (cursor position relative to the camera). Regions nest
/// (a tavern inside the town), so this looks at the cell, not the bounding rectangle; a click
/// on a wall or doorway counts as the room behind it.
pub fn region_at(world: &WorldSnapshot, overlay: Vec2) -> Option<String> {
    let cell = MapFrame::of(world).cell(overlay);
    if let Some(r) = world.world.region_at(cell) {
        return Some(r.name.clone());
    }
    let around = [IVec2::X, IVec2::NEG_X, IVec2::Y, IVec2::NEG_Y];
    let near: Vec<_> = around.iter().filter_map(|d| world.world.region_at(cell + *d)).collect();
    near.iter().find(|r| r.indoor).or(near.first()).map(|r| r.name.clone())
}

/// Close the map and walk to the middle of a region.
pub fn walk_to_region(target: &str, world: &WorldSnapshot, grid: &WalkGrid, here: Option<IVec2>, ui: &mut UiState) {
    let (Some(region), Some(here)) = (world.region(target), here) else {
        return;
    };
    if region.contains(here) {
        ui.toast(format!("You are already in {target}."));
        return;
    }
    let center = region.center();
    let goals: HashSet<IVec2> = (-ARRIVE_RADIUS..=ARRIVE_RADIUS)
        .flat_map(|dx| (-ARRIVE_RADIUS..=ARRIVE_RADIUS).map(move |dy| center + IVec2::new(dx, dy)))
        .filter(|c| region.contains(*c))
        .collect();
    match grid.path(here, &goals) {
        Some(path) => {
            ui.cancel_travel();
            ui.toast(format!("Walking to {target}..."));
            ui.auto_walk = path;
            ui.travel_to = Some(target.to_string());
            ui.world_map = None;
        }
        None => ui.toast(format!("No way through to {target}.")),
    }
}

pub fn render_world_map(
    mut commands: Commands,
    world: Res<WorldSnapshot>,
    ui: Res<UiState>,
    mut state: ResMut<WorldMapState>,
    existing: Query<Entity, With<WorldMapEntity>>,
    camera: Query<Entity, With<Camera2d>>,
    ground: Res<RegionImages>,
    structures: Res<StructureImage>,
) {
    let key = ui.world_map.as_ref().map(|v| (world.revision, v.selected.clone()));
    if key == state.key {
        return;
    }
    state.key = key;
    for entity in &existing {
        commands.entity(entity).despawn();
    }
    let (Some(view), Ok(camera)) = (ui.world_map.as_ref(), camera.single()) else {
        return;
    };
    let frame = MapFrame::of(&world);
    let here = world.current_location.name.as_str();

    // Children of the camera, so the overlay stays on screen wherever the player is.
    commands.entity(camera).with_children(|parent| {
        parent.spawn((
            Sprite {
                color: Color::srgba(0.03, 0.05, 0.07, 0.96),
                custom_size: Some(Vec2::new(1400.0, 900.0)),
                ..default()
            },
            Transform::from_xyz(0.0, 0.0, MAP_Z),
            WorldMapEntity,
        ));
        label(parent, Vec2::new(0.0, 300.0), "WORLD MAP    click a region to walk there   right-click to close", 18.0);
        for region in &world.world.regions {
            let (center, size) = frame.region_rect(region);
            let color = if region.name == view.selected {
                Color::srgb(0.45, 0.38, 0.16)
            } else if region.name == here {
                Color::srgb(0.12, 0.30, 0.42)
            } else {
                Color::srgb(0.16, 0.19, 0.22)
            };
            parent.spawn((
                Sprite {
                    image: ground.get(&region.name).unwrap_or_default(),
                    color,
                    custom_size: Some(size),
                    ..default()
                },
                Transform::from_xyz(center.x, center.y, MAP_Z + 1.0),
                WorldMapEntity,
            ));
        }
        if let Some(image) = structures.get() {
            let size = Vec2::new(world.world.width as f32, world.world.height as f32) * frame.scale;
            let center = frame.point(0.0, 0.0) + Vec2::new(size.x, -size.y) * 0.5;
            parent.spawn((
                Sprite {
                    image,
                    custom_size: Some(size),
                    ..default()
                },
                Transform::from_xyz(center.x, center.y, MAP_Z + 1.5),
                WorldMapEntity,
            ));
        }
        for region in &world.world.regions {
            let mid = region.center();
            let pos = frame.point(mid.x as f32 + 0.5, mid.y as f32 + 0.5);
            let marker = if region.name == here { "\n(you are here)" } else { "" };
            label(parent, pos, &format!("{}{marker}", region.name), if region.indoor { 11.0 } else { 14.0 });
        }
        for person in &world.world.persons {
            let Some(cell) = person.cell() else { continue };
            let pos = frame.point(cell.x as f32 + 0.5, cell.y as f32 + 0.5);
            let color = if person.name == world.player.name {
                Color::srgb(0.25, 0.72, 1.0)
            } else {
                Color::srgb(0.85, 0.40, 0.50)
            };
            parent.spawn((
                Sprite {
                    color,
                    custom_size: Some(Vec2::splat(8.0)),
                    ..default()
                },
                Transform::from_xyz(pos.x, pos.y, MAP_Z + 2.0),
                WorldMapEntity,
            ));
            label(parent, pos + Vec2::new(0.0, -10.0), &person.name, 10.0);
        }
        if let Some(region) = world.region(&view.selected) {
            let desc = if region.description.is_empty() { "" } else { region.description.as_str() };
            let inside = match (region.indoor, region.parent.is_empty()) {
                (true, false) => format!("  (indoors, in {})", region.parent),
                (true, true) => "  (indoors)".to_string(),
                _ => String::new(),
            };
            label(parent, Vec2::new(0.0, -300.0), &format!("{}{inside}\n{}", region.name, desc), 15.0);
        }
    });
}

fn label(parent: &mut ChildSpawnerCommands, pos: Vec2, text: &str, size: f32) {
    parent.spawn((
        Text2d::new(text),
        TextFont {
            font_size: size,
            ..default()
        },
        TextColor(Color::srgb(0.92, 0.95, 0.92)),
        Transform::from_xyz(pos.x, pos.y, MAP_Z + 3.0),
        WorldMapEntity,
    ));
}

pub fn draw_world_map_gizmos(
    mut gizmos: Gizmos,
    world: Res<WorldSnapshot>,
    ui: Res<UiState>,
    camera: Query<&Transform, With<Camera2d>>,
) {
    let (Some(view), Ok(camera)) = (ui.world_map.as_ref(), camera.single()) else {
        return;
    };
    let frame = MapFrame::of(&world);
    let offset = camera.translation.truncate();
    if let Some(region) = world.region(&view.selected) {
        let (center, size) = frame.region_rect(region);
        gizmos.rect_2d(Isometry2d::from_translation(center + offset), size, Color::srgb(0.98, 0.80, 0.30));
    }
}
