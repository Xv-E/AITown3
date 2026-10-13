use bevy::{math::Isometry2d, prelude::*};

use crate::net::{Thing, WorldSnapshot};
use crate::ui::{NearbyInteract, UiState};

pub const ROOM_HALF_W: f32 = 360.0;
pub const ROOM_HALF_H: f32 = 210.0;
pub const ROOM_CENTER: Vec2 = Vec2::new(0.0, -30.0);
pub const PLAYER_SPEED: f32 = 220.0;
pub const PLAYER_START: Vec3 = Vec3::new(0.0, -70.0, 10.0);

#[derive(Component)]
pub struct PlayerAvatar;

/// Static room decoration (floor, exits, items); rebuilt on every state sync.
#[derive(Component)]
pub struct SceneEntity;

#[derive(Component, Clone)]
pub struct Interactive {
    pub kind: TargetKind,
    pub prompt: String,
    pub name: String,
    pub radius: f32,
}

#[derive(Clone)]
pub enum TargetKind {
    Exit { to: String },
    Person { name: String },
    Item { thing: Thing },
}

#[derive(Resource, Default)]
pub struct SceneState {
    revision: Option<u64>,
    room: String,
}

const DEFAULT_GRID: IVec2 = IVec2::new(20, 12);
/// Border cells kept free for exits (must match the server's EDGE_MARGIN).
const GRID_MARGIN: i32 = 1;

/// The current zone's cell grid, scaled to fit the room rectangle. Cell (0,0) is top-left.
#[derive(Clone, Copy)]
pub struct ZoneGrid {
    pub size: IVec2,
    pub tile: f32,
    origin: Vec2,
}

impl ZoneGrid {
    pub fn of(world: &WorldSnapshot) -> Self {
        let loc = &world.current_location;
        let size = if loc.grid_w > 0 && loc.grid_h > 0 {
            IVec2::new(loc.grid_w, loc.grid_h)
        } else {
            DEFAULT_GRID
        };
        let tile = (ROOM_HALF_W * 2.0 / size.x as f32).min(ROOM_HALF_H * 2.0 / size.y as f32);
        let px = size.as_vec2() * tile;
        Self {
            size,
            tile,
            origin: ROOM_CENTER + Vec2::new(-px.x * 0.5, px.y * 0.5),
        }
    }

    pub fn pixel_size(&self) -> Vec2 {
        self.size.as_vec2() * self.tile
    }

    pub fn center_of(&self, cell: IVec2, footprint: IVec2) -> Vec2 {
        let c = cell.as_vec2() + footprint.as_vec2() * 0.5;
        self.origin + Vec2::new(c.x * self.tile, -c.y * self.tile)
    }

    pub fn cell_at(&self, pos: Vec2) -> IVec2 {
        let rel = (pos - self.origin) / self.tile;
        IVec2::new(rel.x.floor() as i32, (-rel.y).floor() as i32)
    }

    /// Where a building of `footprint` would go: centred just above the player.
    pub fn build_cell(&self, player: Vec2, footprint: IVec2) -> IVec2 {
        let at = self.cell_at(player);
        let cell = IVec2::new(at.x - (footprint.x - 1) / 2, at.y - footprint.y);
        cell.clamp(
            IVec2::splat(GRID_MARGIN),
            (self.size - footprint - IVec2::splat(GRID_MARGIN)).max(IVec2::splat(GRID_MARGIN)),
        )
    }

    pub fn can_place(&self, world: &WorldSnapshot, cell: IVec2, footprint: IVec2) -> bool {
        let max = cell + footprint;
        if cell.x < GRID_MARGIN
            || cell.y < GRID_MARGIN
            || max.x > self.size.x - GRID_MARGIN
            || max.y > self.size.y - GRID_MARGIN
        {
            return false;
        }
        world.items_here.iter().filter(|t| t.is_fixture()).all(|t| {
            let Some(other) = t.cell() else {
                return true;
            };
            let other_max = other + t.footprint();
            max.x <= other.x || other_max.x <= cell.x || max.y <= other.y || other_max.y <= cell.y
        })
    }
}

pub fn setup_scene(mut commands: Commands) {
    commands.spawn(Camera2d);
    commands.spawn((
        Sprite {
            color: Color::srgb(0.25, 0.72, 1.0),
            custom_size: Some(Vec2::splat(28.0)),
            ..default()
        },
        Transform::from_translation(PLAYER_START),
        PlayerAvatar,
    ));
}

pub fn render_scene(
    mut commands: Commands,
    world: Res<WorldSnapshot>,
    mut scene: ResMut<SceneState>,
    existing: Query<Entity, With<SceneEntity>>,
) {
    if scene.revision == Some(world.revision) {
        return;
    }
    scene.revision = Some(world.revision);
    for entity in &existing {
        commands.entity(entity).despawn();
    }

    let grid = ZoneGrid::of(&world);
    spawn_block(
        &mut commands,
        ROOM_CENTER,
        Vec2::new(ROOM_HALF_W * 2.0, ROOM_HALF_H * 2.0),
        -4.0,
        Color::srgb(0.08, 0.10, 0.12),
        "",
        None,
    );
    spawn_block(
        &mut commands,
        ROOM_CENTER,
        grid.pixel_size(),
        -3.0,
        Color::srgb(0.12, 0.15, 0.18),
        "",
        None,
    );

    for (exit, position) in exit_layout(&world.adjacent) {
        spawn_block(
            &mut commands,
            position,
            Vec2::new(112.0, 42.0),
            2.0,
            Color::srgb(0.20, 0.37, 0.55),
            &format!("Exit\n{exit}"),
            Some(Interactive {
                kind: TargetKind::Exit { to: exit.clone() },
                prompt: format!("Exit to {exit}"),
                name: exit.clone(),
                radius: 78.0,
            }),
        );
    }

    let mut items = world.items_here.clone();
    items.sort_by(|a, b| a.name.cmp(&b.name));
    for (idx, thing) in items.iter().enumerate() {
        let (position, size) = match thing.cell() {
            Some(cell) if thing.is_fixture() => {
                let fp = thing.footprint();
                (grid.center_of(cell, fp), fp.as_vec2() * grid.tile - Vec2::splat(4.0))
            }
            Some(cell) => (grid.center_of(cell, IVec2::ONE), Vec2::splat(grid.tile * 0.7)),
            None if thing.is_door() => (item_position(thing, idx), Vec2::new(78.0, 34.0)),
            None => (item_position(thing, idx), Vec2::new(46.0, 46.0)),
        };
        let color = if thing.locked {
            Color::srgb(0.74, 0.31, 0.18)
        } else if thing.is_door() {
            Color::srgb(0.33, 0.46, 0.58)
        } else if thing.item_kind.eq_ignore_ascii_case("site") {
            Color::srgb(0.62, 0.50, 0.28)
        } else if thing.portable {
            Color::srgb(0.31, 0.64, 0.38)
        } else {
            Color::srgb(0.48, 0.52, 0.56)
        };
        spawn_block(
            &mut commands,
            position,
            size,
            3.0,
            color,
            &item_label(thing),
            Some(Interactive {
                kind: TargetKind::Item {
                    thing: thing.clone(),
                },
                prompt: thing.name.clone(),
                name: thing.name.clone(),
                radius: 50.0_f32.max(size.length() * 0.5 + 24.0),
            }),
        );
    }
}

fn spawn_block(
    commands: &mut Commands,
    position: Vec2,
    size: Vec2,
    z: f32,
    color: Color,
    label: &str,
    interaction: Option<Interactive>,
) {
    let mut entity = commands.spawn((
        Sprite {
            color,
            custom_size: Some(size),
            ..default()
        },
        Transform::from_xyz(position.x, position.y, z),
        SceneEntity,
    ));
    if let Some(interaction) = interaction {
        entity.insert(interaction);
    }
    if !label.is_empty() {
        commands.spawn((
            Text2d::new(label),
            TextFont {
                font_size: 11.0,
                ..default()
            },
            TextColor(Color::srgb(0.92, 0.95, 0.92)),
            Transform::from_xyz(position.x, position.y - size.y * 0.5 - 14.0, z + 0.2),
            SceneEntity,
        ));
    }
}

/// When the player arrives in a new room, put them next to the exit they came through.
pub fn place_player_in_room(
    world: Res<WorldSnapshot>,
    mut scene: ResMut<SceneState>,
    mut player: Query<&mut Transform, With<PlayerAvatar>>,
) {
    if world.current_location.name == scene.room {
        return;
    }
    let previous = std::mem::replace(&mut scene.room, world.current_location.name.clone());
    let Ok(mut transform) = player.single_mut() else {
        return;
    };
    let spot = exit_position_for(&world, &previous)
        .map(|exit| exit + (ROOM_CENTER - exit).normalize_or_zero() * 70.0)
        .unwrap_or(PLAYER_START.truncate());
    transform.translation = spot.extend(PLAYER_START.z);
}

pub fn player_input(
    keys: Res<ButtonInput<KeyCode>>,
    time: Res<Time>,
    ui: Res<UiState>,
    mut query: Query<&mut Transform, With<PlayerAvatar>>,
) {
    if ui.blocks_movement() {
        return;
    }
    let Ok(mut transform) = query.single_mut() else {
        return;
    };
    let mut direction = Vec2::ZERO;
    if keys.pressed(KeyCode::KeyA) || keys.pressed(KeyCode::ArrowLeft) {
        direction.x -= 1.0;
    }
    if keys.pressed(KeyCode::KeyD) || keys.pressed(KeyCode::ArrowRight) {
        direction.x += 1.0;
    }
    if keys.pressed(KeyCode::KeyW) || keys.pressed(KeyCode::ArrowUp) {
        direction.y += 1.0;
    }
    if keys.pressed(KeyCode::KeyS) || keys.pressed(KeyCode::ArrowDown) {
        direction.y -= 1.0;
    }
    if direction != Vec2::ZERO {
        let delta = direction.normalize() * PLAYER_SPEED * time.delta_secs();
        transform.translation.x += delta.x;
        transform.translation.y += delta.y;
        transform.translation = clamp_to_room(transform.translation.truncate())
            .extend(transform.translation.z);
    }
}

pub fn clamp_to_room(pos: Vec2) -> Vec2 {
    pos.clamp(
        Vec2::new(-ROOM_HALF_W + 24.0, ROOM_CENTER.y - ROOM_HALF_H + 24.0),
        Vec2::new(ROOM_HALF_W - 24.0, ROOM_CENTER.y + ROOM_HALF_H - 24.0),
    )
}

pub fn draw_room_gizmos(
    mut gizmos: Gizmos,
    nearby: Res<NearbyInteract>,
    ui: Res<UiState>,
    world: Res<WorldSnapshot>,
    player: Query<&Transform, With<PlayerAvatar>>,
) {
    if ui.world_map.is_some() {
        return;
    }
    gizmos.rect_2d(
        Isometry2d::from_translation(ROOM_CENTER),
        Vec2::new(ROOM_HALF_W * 2.0, ROOM_HALF_H * 2.0),
        Color::srgb(0.36, 0.42, 0.49),
    );
    let grid = ZoneGrid::of(&world);
    let line = Color::srgba(0.5, 0.6, 0.7, if ui.build.is_some() { 0.22 } else { 0.07 });
    let top_left = grid.center_of(IVec2::ZERO, IVec2::ZERO);
    let span = grid.pixel_size();
    for x in 0..=grid.size.x {
        let px = top_left.x + x as f32 * grid.tile;
        gizmos.line_2d(Vec2::new(px, top_left.y), Vec2::new(px, top_left.y - span.y), line);
    }
    for y in 0..=grid.size.y {
        let py = top_left.y - y as f32 * grid.tile;
        gizmos.line_2d(Vec2::new(top_left.x, py), Vec2::new(top_left.x + span.x, py), line);
    }
    if let (Some(build), Ok(player)) = (ui.build.as_ref(), player.single()) {
        let fp = build.footprint();
        let cell = grid.build_cell(player.translation.truncate(), fp);
        let color = if grid.can_place(&world, cell, fp) {
            Color::srgb(0.35, 0.95, 0.45)
        } else {
            Color::srgb(0.95, 0.30, 0.25)
        };
        let size = fp.as_vec2() * grid.tile;
        let center = grid.center_of(cell, fp);
        gizmos.rect_2d(Isometry2d::from_translation(center), size, color);
        gizmos.rect_2d(Isometry2d::from_translation(center), size - Vec2::splat(6.0), color);
        return;
    }
    if nearby.target.is_some() && !ui.blocks_movement() {
        gizmos.circle_2d(
            Isometry2d::from_translation(nearby.position),
            34.0,
            Color::srgb(0.96, 0.78, 0.28),
        );
    }
}

pub fn exit_position_for(world: &WorldSnapshot, location: &str) -> Option<Vec2> {
    exit_layout(&world.adjacent)
        .into_iter()
        .find(|(name, _)| name == location)
        .map(|(_, pos)| pos)
}

/// Idle spot for an NPC: a free grid cell near a per-name preferred cell.
pub fn npc_home(world: &WorldSnapshot, name: &str, idx: usize) -> Vec2 {
    let grid = ZoneGrid::of(world);
    let inner = (grid.size - IVec2::splat(GRID_MARGIN * 2 + 1)).max(IVec2::ONE);
    let preferred = IVec2::new(
        GRID_MARGIN + stable_slot(name, idx, inner.x as usize) as i32,
        GRID_MARGIN + stable_slot(&format!("{name}#"), idx, inner.y as usize) as i32,
    );
    let mut best: Option<(i32, IVec2)> = None;
    for x in GRID_MARGIN..grid.size.x - GRID_MARGIN {
        for y in GRID_MARGIN..grid.size.y - GRID_MARGIN {
            let cell = IVec2::new(x, y);
            if !grid.can_place(world, cell, IVec2::ONE) {
                continue;
            }
            let d = (cell - preferred).abs().element_sum();
            if best.is_none_or(|(bd, _)| d < bd) {
                best = Some((d, cell));
            }
        }
    }
    best.map(|(_, cell)| grid.center_of(cell, IVec2::ONE))
        .unwrap_or(ROOM_CENTER)
}

fn item_position(thing: &Thing, idx: usize) -> Vec2 {
    if thing.is_door() {
        return match stable_slot(&thing.name, idx, 4) {
            0 => Vec2::new(205.0, ROOM_HALF_H - 86.0),
            1 => Vec2::new(ROOM_HALF_W - 90.0, -88.0),
            2 => Vec2::new(205.0, -ROOM_HALF_H - 4.0),
            _ => Vec2::new(-ROOM_HALF_W + 90.0, -88.0),
        };
    }
    let index = stable_slot(&thing.name, idx, 10);
    let column = (index % 5) as f32;
    let row = (index / 5) as f32;
    Vec2::new(-265.0 + column * 74.0, -118.0 - row * 62.0)
}

/// Exits are spread over the four walls (least-used wall first, name hash as the
/// tie-breaker) and spaced evenly along each wall, so they never overlap.
fn exit_layout(exits: &[String]) -> Vec<(String, Vec2)> {
    let mut names: Vec<&String> = exits.iter().collect();
    names.sort();
    names.dedup();

    let mut walls: [Vec<&String>; 4] = Default::default();
    for name in names {
        let preferred = stable_slot(name, 0, 4);
        let wall = (0..4)
            .map(|step| (preferred + step) % 4)
            .min_by_key(|&w| walls[w].len())
            .unwrap_or(preferred);
        walls[wall].push(name);
    }

    let top = ROOM_CENTER.y + ROOM_HALF_H;
    let bottom = ROOM_CENTER.y - ROOM_HALF_H;
    let mut out = Vec::new();
    for (wall, members) in walls.iter().enumerate() {
        let n = members.len();
        for (i, name) in members.iter().enumerate() {
            let t = (i as f32 + 1.0) / (n as f32 + 1.0);
            let pos = match wall {
                0 => Vec2::new(lerp(-ROOM_HALF_W + 120.0, ROOM_HALF_W - 120.0, t), top),
                1 => Vec2::new(ROOM_HALF_W - 58.0, lerp(top - 80.0, bottom + 80.0, t)),
                2 => Vec2::new(lerp(ROOM_HALF_W - 120.0, -ROOM_HALF_W + 120.0, t), bottom),
                _ => Vec2::new(-ROOM_HALF_W + 58.0, lerp(bottom + 80.0, top - 80.0, t)),
            };
            out.push(((*name).clone(), pos));
        }
    }
    out
}

fn lerp(a: f32, b: f32, t: f32) -> f32 {
    a + (b - a) * t
}

fn stable_slot(name: &str, fallback_index: usize, modulo: usize) -> usize {
    if modulo == 0 {
        return 0;
    }
    let mut hash = 5381usize;
    for byte in name.bytes() {
        hash = hash.wrapping_mul(33).wrapping_add(byte as usize);
    }
    (hash.wrapping_add(fallback_index)) % modulo
}

fn item_label(thing: &Thing) -> String {
    if thing.cell().is_some() && thing.is_fixture() {
        return thing.name.clone();
    }
    if thing.item_kind.is_empty() {
        thing.name.clone()
    } else if thing.locked {
        format!("{} ({}, locked)", thing.name, thing.item_kind)
    } else {
        format!("{} ({})", thing.name, thing.item_kind)
    }
}
