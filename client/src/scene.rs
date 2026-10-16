//! The seamless world: one big grid of cells. Regions (Forest, Clearing, …) are named
//! areas on it with no walls between them; the player walks anywhere that is open.

use bevy::{math::Isometry2d, prelude::*};
use std::collections::{BinaryHeap, HashMap, HashSet, VecDeque};
use std::cmp::Reverse;
use std::time::{Duration, Instant};

use crate::icons::{icon_for, IconAtlas};
use crate::terrain::RegionImages;
use crate::net::{NetBridge, Thing, WorldSnapshot};
use crate::ui::{NearbyInteract, UiState};

/// Pixels per world cell.
pub const TILE: f32 = 32.0;
pub const PLAYER_SPEED: f32 = 8.0 * TILE;
/// How often the player's cell is reported to the server while walking.
const REPORT_EVERY: Duration = Duration::from_millis(200);
/// The server put the player further than this (cells) from anywhere we reported recently:
/// it moved them on purpose (world reload, a story teleport), so follow it.
const SNAP_CELLS: i32 = 4;
/// Pushed state can lag our reports by a second or more; positions we reported within this
/// window are echoes of our own walking, not the server moving us.
const ECHO_WINDOW: Duration = Duration::from_secs(5);
const CAMERA_FOLLOW: f32 = 6.0;

const REGION_COLORS: [(f32, f32, f32); 6] = [
    (0.13, 0.17, 0.13),
    (0.15, 0.14, 0.11),
    (0.11, 0.15, 0.17),
    (0.16, 0.12, 0.14),
    (0.12, 0.13, 0.17),
    (0.14, 0.16, 0.11),
];

#[derive(Component)]
pub struct PlayerAvatar;

/// Static world decoration (floors, items, labels); rebuilt on every state sync.
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
    Person { name: String },
    Item { thing: Thing },
}

/// Centre of a world cell in screen space (cell y grows downwards, screen y upwards).
pub fn cell_center(cell: IVec2) -> Vec2 {
    Vec2::new((cell.x as f32 + 0.5) * TILE, -(cell.y as f32 + 0.5) * TILE)
}

pub fn cell_at(pos: Vec2) -> IVec2 {
    IVec2::new((pos.x / TILE).floor() as i32, (-pos.y / TILE).floor() as i32)
}

fn footprint_center(cell: IVec2, size: IVec2) -> Vec2 {
    Vec2::new(
        (cell.x as f32 + size.x as f32 * 0.5) * TILE,
        -(cell.y as f32 + size.y as f32 * 0.5) * TILE,
    )
}

// ---------------------------------------------------------------------------
// Walkable cells (mirrors the server: all regions minus fixtures)
// ---------------------------------------------------------------------------

#[derive(Resource, Default)]
pub struct WalkGrid {
    revision: Option<u64>,
    walkable: HashSet<IVec2>,
}

impl WalkGrid {
    /// Offline / no layout yet: let the player roam freely.
    pub fn is_open(&self, cell: IVec2) -> bool {
        self.walkable.is_empty() || self.walkable.contains(&cell)
    }

    pub fn has_layout(&self) -> bool {
        !self.walkable.is_empty()
    }

    /// A* over open cells (8 directions, no corner cutting); path excludes `from`.
    pub fn path(&self, from: IVec2, goals: &HashSet<IVec2>) -> Option<Vec<IVec2>> {
        let goals: HashSet<IVec2> = goals.iter().copied().filter(|g| self.is_open(*g)).collect();
        if goals.is_empty() {
            return None;
        }
        let h = |c: IVec2| goals.iter().map(|g| octile(c, *g)).fold(f32::MAX, f32::min);
        let key = |f: f32| (f * 1000.0) as i64;
        let mut open = BinaryHeap::from([Reverse((key(h(from)), from.x, from.y))]);
        let mut came: HashMap<IVec2, IVec2> = HashMap::new();
        let mut cost: HashMap<IVec2, f32> = HashMap::from([(from, 0.0)]);
        while let Some(Reverse((_, x, y))) = open.pop() {
            let cur = IVec2::new(x, y);
            if goals.contains(&cur) {
                let mut path = vec![cur];
                while let Some(prev) = came.get(path.last().unwrap()) {
                    if *prev == from {
                        break;
                    }
                    path.push(*prev);
                }
                path.reverse();
                return Some(path);
            }
            let g = cost[&cur];
            for (dx, dy) in [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)] {
                let next = IVec2::new(cur.x + dx, cur.y + dy);
                if !self.is_open(next) {
                    continue;
                }
                if dx != 0 && dy != 0
                    && (!self.is_open(IVec2::new(cur.x + dx, cur.y)) || !self.is_open(IVec2::new(cur.x, cur.y + dy)))
                {
                    continue;
                }
                let ng = g + if dx != 0 && dy != 0 { 1.4142 } else { 1.0 };
                if ng < *cost.get(&next).unwrap_or(&f32::MAX) {
                    cost.insert(next, ng);
                    came.insert(next, cur);
                    open.push(Reverse((key(ng + h(next)), next.x, next.y)));
                }
            }
            if cost.len() > 20_000 {
                break;
            }
        }
        None
    }
}

fn octile(a: IVec2, b: IVec2) -> f32 {
    let d = (a - b).abs();
    d.x.max(d.y) as f32 + 0.4142 * d.x.min(d.y) as f32
}

pub fn rebuild_walk_grid(world: Res<WorldSnapshot>, mut grid: ResMut<WalkGrid>) {
    if grid.revision == Some(world.revision) {
        return;
    }
    grid.revision = Some(world.revision);
    let mut open = HashSet::new();
    for r in &world.world.regions {
        for x in r.x..r.x + r.w {
            for y in r.y..r.y + r.h {
                let cell = IVec2::new(x, y);
                if r.contains(cell) {
                    open.insert(cell);
                }
            }
        }
    }
    for item in &world.world.items {
        let Some(cell) = item.cell() else { continue };
        if item.portable {
            continue;
        }
        let size = item.footprint();
        for dx in 0..size.x {
            for dy in 0..size.y {
                open.remove(&(cell + IVec2::new(dx, dy)));
            }
        }
    }
    grid.walkable = open;
}

// ---------------------------------------------------------------------------
// Build placement grid: the region the player stands in (item cells are local to it)
// ---------------------------------------------------------------------------

const DEFAULT_GRID: IVec2 = IVec2::new(20, 12);
/// Border cells kept free (must match the server's EDGE_MARGIN).
const GRID_MARGIN: i32 = 1;

#[derive(Clone, Copy)]
pub struct ZoneGrid {
    pub size: IVec2,
    pub tile: f32,
    origin: Vec2,
}

impl ZoneGrid {
    pub fn of(world: &WorldSnapshot) -> Self {
        match world.region(&world.current_location.name) {
            Some(r) => Self {
                size: IVec2::new(r.w, r.h),
                tile: TILE,
                origin: Vec2::new(r.x as f32 * TILE, -(r.y as f32) * TILE),
            },
            None => Self {
                size: DEFAULT_GRID,
                tile: TILE,
                origin: Vec2::ZERO,
            },
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
        // Every cell of it on the region's ground, not in a cut-away corner.
        if let Some(region) = world.region(&world.current_location.name) {
            let corner = IVec2::new(region.x, region.y) + cell;
            let on_ground = (0..footprint.x)
                .all(|dx| (0..footprint.y).all(|dy| region.contains(corner + IVec2::new(dx, dy))));
            if !on_ground {
                return false;
            }
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

// ---------------------------------------------------------------------------
// Rendering the world
// ---------------------------------------------------------------------------

#[derive(Resource, Default)]
pub struct SceneState {
    revision: Option<u64>,
}

pub fn setup_scene(mut commands: Commands) {
    commands.spawn(Camera2d);
    commands.spawn((
        Sprite {
            color: Color::srgb(0.25, 0.72, 1.0),
            custom_size: Some(Vec2::splat(TILE * 0.8)),
            ..default()
        },
        Transform::from_xyz(0.0, 0.0, 10.0),
        PlayerAvatar,
    ));
}

pub fn render_scene(
    mut commands: Commands,
    world: Res<WorldSnapshot>,
    mut scene: ResMut<SceneState>,
    icons: Res<IconAtlas>,
    ground: Res<RegionImages>,
    existing: Query<Entity, With<SceneEntity>>,
) {
    if scene.revision == Some(world.revision) {
        return;
    }
    scene.revision = Some(world.revision);
    for entity in &existing {
        commands.entity(entity).despawn();
    }

    if world.world.regions.is_empty() {
        render_offline_room(&mut commands, &world, &icons);
        return;
    }

    for (i, region) in world.world.regions.iter().enumerate() {
        let (r, g, b) = REGION_COLORS[i % REGION_COLORS.len()];
        let size = Vec2::new(region.w as f32, region.h as f32) * TILE;
        let center = Vec2::new(region.x as f32 * TILE, -(region.y as f32) * TILE) + Vec2::new(size.x, -size.y) * 0.5;
        commands.spawn((
            Sprite {
                image: ground.get(&region.name).unwrap_or_default(),
                color: Color::srgb(r, g, b),
                custom_size: Some(size),
                ..default()
            },
            Transform::from_xyz(center.x, center.y, -4.0),
            SceneEntity,
        ));
        let corner = Vec2::new(region.x as f32 * TILE, -(region.y as f32) * TILE);
        commands.spawn((
            Text2d::new(region.name.clone()),
            TextFont {
                font_size: 18.0,
                ..default()
            },
            TextColor(Color::srgba(0.85, 0.90, 0.85, 0.55)),
            Transform::from_xyz(corner.x + size.x * 0.5, corner.y - 16.0, -3.0),
            SceneEntity,
        ));
    }

    let mut items = world.world.items.clone();
    items.sort_by(|a, b| a.name.cmp(&b.name));
    for thing in &items {
        let Some(cell) = thing.cell() else { continue };
        let (position, size) = if thing.is_fixture() {
            let fp = thing.footprint();
            (footprint_center(cell, fp), fp.as_vec2() * TILE - Vec2::splat(4.0))
        } else {
            (cell_center(cell), Vec2::splat(TILE * 0.6))
        };
        spawn_thing(&mut commands, &icons, thing, position, size);
    }
}

/// Offline demo: no world layout from the server, just the snapshot's items in a row.
fn render_offline_room(commands: &mut Commands, world: &WorldSnapshot, icons: &IconAtlas) {
    let size = DEFAULT_GRID.as_vec2() * TILE;
    commands.spawn((
        Sprite {
            color: Color::srgb(0.12, 0.15, 0.18),
            custom_size: Some(size),
            ..default()
        },
        Transform::from_xyz(0.0, 0.0, -4.0),
        SceneEntity,
    ));
    for (i, thing) in world.items_here.iter().enumerate() {
        let pos = Vec2::new(-size.x * 0.4 + i as f32 * TILE * 2.5, -TILE * 3.0);
        spawn_thing(commands, icons, thing, pos, Vec2::splat(TILE * 0.8));
    }
}

fn spawn_thing(commands: &mut Commands, icons: &IconAtlas, thing: &Thing, position: Vec2, size: Vec2) {
    // Fixtures keep a faint footprint (where they stand); locked things get a red one.
    let footing = if thing.locked {
        Some(Color::srgba(0.74, 0.31, 0.18, 0.55))
    } else if thing.is_fixture() && size.max_element() > TILE {
        Some(Color::srgba(0.0, 0.0, 0.0, 0.18))
    } else {
        None
    };
    if let Some(color) = footing {
        commands.spawn((
            Sprite { color, custom_size: Some(size), ..default() },
            Transform::from_xyz(position.x, position.y, 2.9),
            SceneEntity,
        ));
    }
    let side = if thing.is_fixture() { size.min_element().min(TILE * 2.0).max(TILE * 0.9) } else { TILE * 0.85 };
    commands.spawn((
        Sprite {
            image: icons.get(icon_for(thing)),
            custom_size: Some(Vec2::splat(side)),
            ..default()
        },
        Transform::from_xyz(position.x, position.y, 3.0),
        SceneEntity,
        Interactive {
            kind: TargetKind::Item { thing: thing.clone() },
            prompt: thing.name.clone(),
            name: thing.name.clone(),
            radius: size.max_element() * 0.5 + TILE * 1.3,
        },
    ));
    commands.spawn((
        Text2d::new(item_label(thing)),
        TextFont {
            font_size: 11.0,
            ..default()
        },
        TextColor(Color::srgb(0.92, 0.95, 0.92)),
        Transform::from_xyz(position.x, position.y - size.y * 0.5 - 10.0, 3.2),
        SceneEntity,
    ));
}

fn item_label(thing: &Thing) -> String {
    if thing.is_fixture() || thing.item_kind.is_empty() {
        thing.name.clone()
    } else if thing.locked {
        format!("{} ({}, locked)", thing.name, thing.item_kind)
    } else {
        format!("{} ({})", thing.name, thing.item_kind)
    }
}

// ---------------------------------------------------------------------------
// Player: free walking, auto-walk, position sync, camera
// ---------------------------------------------------------------------------

#[derive(Resource)]
pub struct PlayerSync {
    /// Which world the avatar was placed in (rebuilding the world re-places it).
    placed_in: Option<String>,
    last_sent_cell: Option<IVec2>,
    last_sent_at: Instant,
    /// Cells reported recently (newest last), to recognise the server echoing them back.
    recent: VecDeque<(IVec2, Instant)>,
    /// Only compare when a new state arrives, not every frame against the same old one.
    revision: Option<u64>,
}

impl Default for PlayerSync {
    fn default() -> Self {
        Self {
            placed_in: None,
            last_sent_cell: None,
            last_sent_at: Instant::now(),
            recent: VecDeque::new(),
            revision: None,
        }
    }
}

impl PlayerSync {
    fn remember(&mut self, cell: IVec2) {
        self.recent.push_back((cell, Instant::now()));
        while self.recent.front().is_some_and(|(_, at)| at.elapsed() > ECHO_WINDOW) {
            self.recent.pop_front();
        }
    }

    /// Is `server` just one of our own recent positions coming back (possibly late)?
    fn is_echo(&self, server: IVec2) -> bool {
        self.recent
            .iter()
            .any(|(cell, at)| at.elapsed() <= ECHO_WINDOW && (*cell - server).abs().max_element() <= SNAP_CELLS)
    }
}

fn world_key(world: &WorldSnapshot) -> String {
    let mut names: Vec<&str> = world.world.regions.iter().map(|r| r.name.as_str()).collect();
    names.sort();
    format!("{}|{}", world.player.name, names.join(","))
}

/// Put the avatar where the server says: on (re)load, or when the server moved the player.
pub fn sync_player_from_server(
    world: Res<WorldSnapshot>,
    mut sync: ResMut<PlayerSync>,
    mut player: Query<&mut Transform, With<PlayerAvatar>>,
) {
    if !world.online || !world.found {
        return;
    }
    if sync.revision == Some(world.revision) {
        return;
    }
    sync.revision = Some(world.revision);
    let Some(server) = world.player_cell() else { return };
    let Ok(mut transform) = player.single_mut() else { return };
    let key = world_key(&world);
    let here = cell_at(transform.translation.truncate());
    let moved_by_server = (here - server).abs().max_element() > SNAP_CELLS && !sync.is_echo(server);
    if sync.placed_in.as_deref() != Some(key.as_str()) || moved_by_server {
        let pos = cell_center(server);
        transform.translation = pos.extend(10.0);
        sync.placed_in = Some(key);
        sync.last_sent_cell = Some(server);
        sync.recent.clear();
        sync.remember(server);
    }
}

pub fn player_input(
    keys: Res<ButtonInput<KeyCode>>,
    time: Res<Time>,
    grid: Res<WalkGrid>,
    mut ui: ResMut<UiState>,
    mut query: Query<&mut Transform, With<PlayerAvatar>>,
) {
    if ui.blocks_movement() {
        return;
    }
    let Ok(mut transform) = query.single_mut() else {
        return;
    };
    let pos = transform.translation.truncate();
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
        ui.cancel_travel();
    } else if let Some(next) = ui.auto_walk.first().copied() {
        let target = cell_center(next);
        if pos.distance(target) < 3.0 {
            ui.auto_walk.remove(0);
            if ui.auto_walk.is_empty() {
                if let Some(dest) = ui.travel_to.take() {
                    ui.toast(format!("Arrived at {dest}."));
                }
            }
            return;
        }
        direction = target - pos;
    }
    if direction == Vec2::ZERO {
        return;
    }
    let step = direction.normalize() * PLAYER_SPEED * time.delta_secs();
    // Slide along walls: try each axis on its own. Stuck off the ground: walk freely back onto it.
    let stranded = !grid.is_open(cell_at(pos));
    let mut next = pos;
    for delta in [Vec2::new(step.x, 0.0), Vec2::new(0.0, step.y)] {
        let candidate = next + delta;
        if stranded || grid.is_open(cell_at(candidate)) {
            next = candidate;
        }
    }
    transform.translation.x = next.x;
    transform.translation.y = next.y;
}

pub fn report_player_position(
    world: Res<WorldSnapshot>,
    grid: Res<WalkGrid>,
    net: Res<NetBridge>,
    mut sync: ResMut<PlayerSync>,
    player: Query<&Transform, With<PlayerAvatar>>,
) {
    if !world.online || !world.found || !grid.has_layout() || sync.placed_in.is_none() {
        return;
    }
    let Ok(transform) = player.single() else { return };
    let cell = cell_at(transform.translation.truncate());
    if sync.last_sent_cell == Some(cell) || sync.last_sent_at.elapsed() < REPORT_EVERY || !grid.is_open(cell) {
        return;
    }
    net.report_position(cell);
    sync.last_sent_cell = Some(cell);
    sync.last_sent_at = Instant::now();
    sync.remember(cell);
}

pub fn camera_follow(
    time: Res<Time>,
    player: Query<&Transform, (With<PlayerAvatar>, Without<Camera2d>)>,
    mut camera: Query<&mut Transform, With<Camera2d>>,
) {
    let (Ok(player), Ok(mut camera)) = (player.single(), camera.single_mut()) else {
        return;
    };
    let target = player.translation.truncate();
    let current = camera.translation.truncate();
    let t = (CAMERA_FOLLOW * time.delta_secs()).min(1.0);
    let next = if current.distance(target) > TILE * 20.0 { target } else { current.lerp(target, t) };
    camera.translation.x = next.x;
    camera.translation.y = next.y;
}

pub fn draw_world_gizmos(
    mut gizmos: Gizmos,
    nearby: Res<NearbyInteract>,
    ui: Res<UiState>,
    world: Res<WorldSnapshot>,
    player: Query<&Transform, With<PlayerAvatar>>,
) {
    if ui.world_map.is_some() {
        return;
    }
    for cell in &ui.auto_walk {
        gizmos.circle_2d(Isometry2d::from_translation(cell_center(*cell)), 3.0, Color::srgba(0.98, 0.80, 0.30, 0.6));
    }
    if let (Some(build), Ok(player)) = (ui.build.as_ref(), player.single()) {
        let grid = ZoneGrid::of(&world);
        let line = Color::srgba(0.5, 0.6, 0.7, 0.22);
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
        let fp = build.footprint();
        let cell = grid.build_cell(ui.build_anchor(player.translation.truncate()), fp);
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
            TILE * 1.1,
            Color::srgb(0.96, 0.78, 0.28),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn grid_of(cells: &[(i32, i32)]) -> WalkGrid {
        WalkGrid {
            revision: None,
            walkable: cells.iter().map(|(x, y)| IVec2::new(*x, *y)).collect(),
        }
    }

    #[test]
    fn late_echoes_of_our_own_walk_do_not_pull_the_player_back() {
        let mut sync = PlayerSync::default();
        for x in 10..30 {
            sync.remember(IVec2::new(x, 5)); // walked 20 cells east
        }
        assert!(sync.is_echo(IVec2::new(12, 5)), "an old position coming back late");
        assert!(!sync.is_echo(IVec2::new(50, 30)), "somewhere we never were: the server moved us");
    }

    #[test]
    fn cell_and_pixel_round_trip() {
        for cell in [IVec2::new(0, 0), IVec2::new(37, 12), IVec2::new(59, 39)] {
            assert_eq!(cell_at(cell_center(cell)), cell);
        }
    }

    #[test]
    fn path_goes_around_blocked_cells() {
        // 3x3 room with the middle blocked: from left-middle to right-middle.
        let open: Vec<(i32, i32)> = (0..3)
            .flat_map(|x| (0..3).map(move |y| (x, y)))
            .filter(|c| *c != (1, 1))
            .collect();
        let grid = grid_of(&open);
        let path = grid.path(IVec2::new(0, 1), &HashSet::from([IVec2::new(2, 1)])).unwrap();
        assert_eq!(path.last(), Some(&IVec2::new(2, 1)));
        assert!(!path.contains(&IVec2::new(1, 1)));
    }
}
