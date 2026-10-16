use bevy::input::mouse::{AccumulatedMouseScroll, MouseScrollUnit};
use bevy::prelude::*;
use bevy::ui::FocusPolicy;
use std::{
    collections::{HashMap, HashSet},
    time::{Duration, Instant},
};

use crate::adapter::thought_line;
use crate::net::{trim_line, Blueprint, History, HistoryEntry, NetBridge, WorldSnapshot};
use crate::scene::{cell_at, cell_center, Interactive, PlayerAvatar, TargetKind, WalkGrid, ZoneGrid, TILE};

const TOAST_DURATION: Duration = Duration::from_secs(4);
const HISTORY_POLL: Duration = Duration::from_secs(1);
/// History entries shown at once (the wheel scrolls through the rest).
const HISTORY_ENTRIES: usize = 40;
/// How close (px) a click must land to a person / thing to count as clicking it.
const CLICK_RADIUS: f32 = 40.0;
const MENU_WIDTH: f32 = 320.0;
/// Chat lines kept on screen (older ones scroll away).
const CHAT_SHOWN: usize = 12;
/// Longest line the player can type.
const CHAT_MAX_CHARS: usize = 200;
/// Re-plan the walk to a moving target this often.
const APPROACH_REPATH: Duration = Duration::from_millis(700);

#[derive(Resource)]
pub struct UiState {
    pub debug_visible: bool,
    pub toast: String,
    pub toast_at: Instant,
    pub dialogue: Option<DialogueView>,
    pub menu: Option<Menu>,
    pub history: Option<HistoryView>,
    pub sim_running: bool,
    /// Blueprint being placed: the ghost follows the player until E / Esc.
    pub build: Option<BuildPlacement>,
    pub world_map: Option<WorldMapView>,
    /// Cells left to walk to a world-map destination (the player walks them automatically).
    pub auto_walk: Vec<IVec2>,
    pub travel_to: Option<String>,
    /// Walking up to this person / thing; its menu opens on arrival.
    pub approach: Option<String>,
    approach_path_at: Option<Instant>,
    /// Where the mouse points in the world (None = outside the window).
    pub cursor_world: Option<Vec2>,
    /// The mouse is over a panel or button: clicks belong to the UI, not the world.
    pub pointer_on_ui: bool,
    /// Screen position the open menu pops up at (None = default spot).
    pub menu_anchor: Option<Vec2>,
}

impl Default for UiState {
    fn default() -> Self {
        Self {
            debug_visible: false,
            toast: String::new(),
            toast_at: Instant::now(),
            dialogue: None,
            menu: None,
            history: None,
            sim_running: false,
            build: None,
            world_map: None,
            auto_walk: Vec::new(),
            travel_to: None,
            approach: None,
            approach_path_at: None,
            cursor_world: None,
            pointer_on_ui: false,
            menu_anchor: None,
        }
    }
}

#[derive(Clone)]
pub struct BuildPlacement {
    pub archetype: String,
    pub label: String,
    pub w: i32,
    pub h: i32,
}

impl BuildPlacement {
    pub fn footprint(&self) -> IVec2 {
        IVec2::new(self.w.max(1), self.h.max(1))
    }
}

pub struct WorldMapView {
    pub selected: String,
}

/// NPC behaviour log panel: decisions with reasons/triggers, actions and results.
pub struct HistoryView {
    pub name: String,
    pub data: Option<History>,
    pub error: String,
    pub scroll: usize,
    pub wheel: f32,
    pub in_flight: bool,
    pub last_fetch: Option<Instant>,
    /// Entries clicked open (by key): they show what was said, why, the plan...
    pub expanded: HashSet<String>,
}

impl HistoryView {
    fn new(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            data: None,
            error: String::new(),
            scroll: 0,
            wheel: 0.0,
            in_flight: false,
            last_fetch: None,
            expanded: HashSet::new(),
        }
    }
}

impl UiState {
    pub fn toast(&mut self, text: impl Into<String>) {
        self.toast = text.into();
        self.toast_at = Instant::now();
    }

    pub fn blocks_movement(&self) -> bool {
        self.menu.is_some() || self.dialogue.is_some() || self.history.is_some() || self.world_map.is_some()
    }

    pub fn travelling(&self) -> bool {
        !self.auto_walk.is_empty()
    }

    pub fn cancel_travel(&mut self) {
        self.auto_walk.clear();
        self.travel_to = None;
        self.approach = None;
    }

    /// Where a building being placed goes: under the mouse, else at the player.
    pub fn build_anchor(&self, player: Vec2) -> Vec2 {
        self.cursor_world.unwrap_or(player)
    }

    fn open_menu(&mut self, menu: Menu, anchor: Option<Vec2>) {
        self.menu = Some(menu);
        self.menu_anchor = anchor;
    }

    pub fn open_build_menu(&mut self, blueprints: Vec<Blueprint>) {
        let mut menu = Menu::new("Build", "Pick a blueprint, then walk to place it");
        for bp in blueprints {
            let size = format!("{}x{}", bp.w, bp.h);
            if bp.available {
                menu.add(
                    format!("{}  ({size})", bp.label),
                    Command::StartBuild(BuildPlacement {
                        archetype: bp.archetype,
                        label: bp.label,
                        w: bp.w,
                        h: bp.h,
                    }),
                );
            } else {
                menu.add(
                    format!("{}  ({size}) - locked", bp.label),
                    Command::Examine {
                        title: bp.label,
                        text: format!("{}\n\nNot yet: {}", bp.description, bp.reason),
                    },
                );
            }
        }
        if menu.options.is_empty() {
            menu.add("(nothing can be built here)", Command::Close);
        }
        menu.add("Close", Command::Close);
        self.open_menu(menu, None);
    }
}

pub struct DialogueView {
    pub partner: String,
    pub lines: Vec<(String, String)>,
    pub waiting: bool,
    /// A conversation the player types into (else: just text to read).
    pub chat: bool,
    pub input: String,
    /// Text still being composed in the input method (e.g. pinyin), not yet committed.
    pub preedit: String,
}

impl DialogueView {
    pub fn shown(partner: impl Into<String>, lines: Vec<(String, String)>) -> Self {
        Self { partner: partner.into(), lines, waiting: false, chat: false, input: String::new(), preedit: String::new() }
    }

    fn chat(partner: impl Into<String>) -> Self {
        Self { chat: true, ..Self::shown(partner, Vec::new()) }
    }
}

#[derive(Resource, Default)]
pub struct NearbyInteract {
    pub target: Option<Interactive>,
    pub position: Vec2,
}

// ---------------------------------------------------------------------------
// Menus
// ---------------------------------------------------------------------------

#[derive(Clone)]
pub struct Menu {
    title: String,
    subtitle: String,
    options: Vec<MenuOption>,
    selected: usize,
    parent: Option<Box<Menu>>,
}

#[derive(Clone)]
struct MenuOption {
    label: String,
    command: Command,
}

#[derive(Clone)]
enum Command {
    Act {
        action: &'static str,
        params: Vec<(&'static str, String)>,
    },
    Talk {
        person: String,
    },
    Examine {
        title: String,
        text: String,
    },
    History {
        person: String,
    },
    StartBuild(BuildPlacement),
    Submenu(Box<Menu>),
    Back,
    Close,
}

impl Menu {
    fn new(title: impl Into<String>, subtitle: impl Into<String>) -> Self {
        Self {
            title: title.into(),
            subtitle: subtitle.into(),
            options: Vec::new(),
            selected: 0,
            parent: None,
        }
    }

    fn add(&mut self, label: impl Into<String>, command: Command) {
        self.options.push(MenuOption {
            label: label.into(),
            command,
        });
    }
}

fn act(action: &'static str, params: &[(&'static str, &str)]) -> Command {
    Command::Act {
        action,
        params: params.iter().map(|(k, v)| (*k, v.to_string())).collect(),
    }
}

fn target_menu(world: &WorldSnapshot, target: &Interactive) -> Menu {
    let me = world.player.name.as_str();
    let here = world.current_location.name.as_str();
    let held = world.held_items();

    match &target.kind {
        TargetKind::Person { name } => {
            let actor = world.person_here(name).or_else(|| world.character(name));
            let alive = actor.is_none_or(|a| a.alive);
            let subtitle = match actor {
                Some(a) if !a.alive => format!("{} (down)", a.role),
                Some(a) if a.mood.is_empty() => format!("{}  HP {}", a.role, a.hp),
                Some(a) => format!("{}  HP {}  mood: {}", a.role, a.hp, a.mood),
                None => String::new(),
            };
            let mut menu = Menu::new(name.clone(), subtitle);
            if alive {
                menu.add("Talk", Command::Talk { person: name.clone() });
                for item in &held {
                    menu.add(
                        format!("Give {item}"),
                        act("give", &[("giver", me), ("i", item), ("receiver", name), ("l", here)]),
                    );
                }
            }
            for item in world.recommended_tools(name) {
                if held.contains(item) {
                    menu.add(
                        format!("Use {item} on {name}  *"),
                        use_on(me, item, name, here),
                    );
                }
            }
            if alive {
                menu.add("Attack", act("attack", &[("attacker", me), ("target", name), ("l", here)]));
            }
            let text = match actor {
                Some(a) if !a.personality.is_empty() => {
                    format!("{name} — {}. {}", a.role, a.personality)
                }
                Some(a) => format!("{name} — {}.", a.role),
                None => format!("{name}."),
            };
            menu.add(
                "Examine",
                Command::Examine {
                    title: name.clone(),
                    text,
                },
            );
            menu.add("History (why they do things)", Command::History { person: name.clone() });
            menu.add("Leave", Command::Close);
            menu
        }
        TargetKind::Item { thing } => {
            let name = thing.name.as_str();
            let mut subtitle = thing.item_kind.clone();
            if thing.locked {
                subtitle = format!("{subtitle} (locked)").trim().to_string();
            }
            let mut menu = Menu::new(name, subtitle);
            if thing.can_pick_up() {
                menu.add("Pick up", act("pick_up", &[("c", me), ("i", name), ("l", here)]));
            }
            // The object's own actions: what you can do now, and why the rest can't be done.
            for action in &thing.actions {
                if action.available {
                    menu.add(
                        action.label.clone(),
                        act("interact", &[("c", me), ("target", name), ("option", &action.id)]),
                    );
                } else {
                    menu.add(
                        format!("({}: not now)", action.label),
                        Command::Examine {
                            title: action.label.clone(),
                            text: action.reason.clone(),
                        },
                    );
                }
            }
            let recommended = world.recommended_tools(name);
            let mut tools: Vec<&String> = held.iter().filter(|t| recommended.contains(t)).collect();
            tools.extend(held.iter().filter(|t| !recommended.contains(t)));
            for tool in tools {
                let mark = if recommended.contains(tool) { "  *" } else { "" };
                menu.add(format!("Use {tool} on {name}{mark}"), use_on(me, tool, name, here));
            }
            let text = if thing.description.is_empty() {
                "Nothing special about it.".to_string()
            } else {
                thing.description.clone()
            };
            menu.add(
                "Examine",
                Command::Examine {
                    title: name.to_string(),
                    text,
                },
            );
            menu.add("Leave", Command::Close);
            menu
        }
    }
}

fn use_on(me: &str, tool: &str, target: &str, here: &str) -> Command {
    act(
        "use_on",
        &[("c", me), ("i", tool), ("target", target), ("from", here), ("to", here)],
    )
}

fn inventory_menu(world: &WorldSnapshot) -> Menu {
    let held = world.held_items();
    let mut menu = Menu::new("Inventory", format!("{} item(s)", held.len()));
    let me = world.player.name.as_str();
    let here = world.current_location.name.as_str();
    for item in &held {
        let mut sub = Menu::new(item.clone(), "In your bag");
        sub.add("Use", act("use_item", &[("c", me), ("i", item)]));
        sub.add("Drop", act("drop", &[("c", me), ("i", item), ("l", here)]));
        sub.add("Back", Command::Back);
        menu.add(item.clone(), Command::Submenu(Box::new(sub)));
    }
    if held.is_empty() {
        menu.add("(empty)", Command::Close);
    }
    menu.add("Close", Command::Close);
    menu
}

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

pub fn detect_nearby(
    player: Query<&Transform, With<PlayerAvatar>>,
    targets: Query<(&Transform, &Interactive), Without<PlayerAvatar>>,
    mut nearby: ResMut<NearbyInteract>,
) {
    let Ok(player_tf) = player.single() else {
        return;
    };
    let player_pos = player_tf.translation.truncate();
    let mut best: Option<(f32, Vec2, &Interactive)> = None;
    for (transform, interactive) in &targets {
        let pos = transform.translation.truncate();
        let dist = player_pos.distance(pos);
        if dist <= interactive.radius && best.is_none_or(|(d, _, _)| dist < d) {
            best = Some((dist, pos, interactive));
        }
    }
    match best {
        Some((_, pos, interactive)) => {
            nearby.position = pos;
            nearby.target = Some(interactive.clone());
        }
        None => nearby.target = None,
    }
}

#[allow(clippy::too_many_arguments)]
pub fn interaction_input(
    keys: Res<ButtonInput<KeyCode>>,
    mouse: Res<ButtonInput<MouseButton>>,
    world: Res<WorldSnapshot>,
    nearby: Res<NearbyInteract>,
    windows: Query<&Window>,
    camera: Query<(&Camera, &GlobalTransform)>,
    camera_at: Query<&Transform, With<Camera2d>>,
    player: Query<&Transform, With<PlayerAvatar>>,
    targets: Query<(&Transform, &Interactive), Without<PlayerAvatar>>,
    hovered: Query<&Interaction>,
    grid: Res<WalkGrid>,
    mut ui: ResMut<UiState>,
    mut net: ResMut<NetBridge>,
) {
    ui.cursor_world = cursor_world(&windows, &camera);
    ui.pointer_on_ui = hovered.iter().any(|i| *i != Interaction::None);
    let screen_cursor = windows.single().ok().and_then(|w| w.cursor_position());
    let left = mouse.just_pressed(MouseButton::Left) && !ui.pointer_on_ui;
    let right = mouse.just_pressed(MouseButton::Right) && !ui.pointer_on_ui;
    let player_pos = player.single().ok().map(|t| t.translation.truncate());

    if keys.just_pressed(KeyCode::Backquote) {
        ui.debug_visible = !ui.debug_visible;
    }

    if ui.dialogue.is_some() {
        let key = [KeyCode::Escape, KeyCode::Space, KeyCode::KeyE, KeyCode::Enter]
            .iter()
            .any(|k| keys.just_pressed(*k));
        let (waiting, chat) = ui.dialogue.as_ref().map_or((false, false), |d| (d.waiting, d.chat));
        if keys.just_pressed(KeyCode::Escape) || right || (!chat && (key || left) && !waiting) {
            ui.dialogue = None;
        }
        return;
    }

    if ui.menu.is_some() {
        if right {
            let parent = ui.menu.as_mut().and_then(|m| m.parent.take());
            ui.menu = parent.map(|p| *p);
            return;
        }
        if !left {
            menu_input(&keys, &world, &mut ui, &net);
            return;
        }
        // Clicked somewhere else: close the menu and handle the click below.
        ui.menu = None;
    }

    if ui.history.is_some() {
        if left || right {
            ui.history = None;
            return;
        }
        history_input(&keys, &world, &mut ui);
        global_keys(&keys, &mut ui, &mut net);
        return;
    }

    if ui.world_map.is_some() {
        let here = player_pos.map(cell_at);
        let hovered_region = match (ui.cursor_world, camera_at.single()) {
            (Some(cursor), Ok(cam)) => crate::world_map::region_at(&world, cursor - cam.translation.truncate()),
            _ => None,
        };
        if let (Some(name), Some(view)) = (hovered_region.as_ref(), ui.world_map.as_mut()) {
            if view.selected != *name {
                view.selected = name.clone();
            }
        }
        if right {
            ui.world_map = None;
            return;
        }
        if left {
            match hovered_region {
                Some(name) => crate::world_map::walk_to_region(&name, &world, &grid, here, &mut ui),
                None => ui.world_map = None,
            }
            return;
        }
        crate::world_map::map_input(&keys, &world, &grid, here, &mut ui);
        global_keys(&keys, &mut ui, &mut net);
        return;
    }

    if ui.build.is_some() {
        global_keys(&keys, &mut ui, &mut net);
        if keys.just_pressed(KeyCode::Escape) || keys.just_pressed(KeyCode::KeyB) || right {
            ui.build = None;
            ui.toast("Building cancelled.");
        } else if keys.just_pressed(KeyCode::KeyE) || keys.just_pressed(KeyCode::Enter) || left {
            let (Some(build), Some(player_pos)) = (ui.build.clone(), player_pos) else {
                return;
            };
            let zone = ZoneGrid::of(&world);
            let cell = zone.build_cell(ui.build_anchor(player_pos), build.footprint());
            if !zone.can_place(&world, cell, build.footprint()) {
                ui.toast("Can't build there - the outline must be red-free (inside the zone, not on another building).");
            } else if ensure_online(&world, &mut ui) {
                net.build(&build.archetype, cell.x, cell.y);
            }
        }
        return;
    }

    global_keys(&keys, &mut ui, &mut net);
    if keys.just_pressed(KeyCode::KeyB) {
        run_ui_action(UiAction::Build, &world, &nearby, &mut ui, &mut net);
        return;
    }
    if keys.just_pressed(KeyCode::KeyM) {
        run_ui_action(UiAction::Map, &world, &nearby, &mut ui, &mut net);
        return;
    }
    if keys.just_pressed(KeyCode::KeyH) {
        run_ui_action(UiAction::History, &world, &nearby, &mut ui, &mut net);
        return;
    }
    if keys.just_pressed(KeyCode::KeyI) || keys.just_pressed(KeyCode::Tab) {
        run_ui_action(UiAction::Bag, &world, &nearby, &mut ui, &mut net);
        return;
    }

    let Some(player_pos) = player_pos else { return };

    if left {
        let Some(cursor) = ui.cursor_world else { return };
        // A person or thing under the mouse: talk / use it, walking over first if needed.
        let mut hit: Option<(f32, Vec2, &Interactive)> = None;
        for (transform, interactive) in &targets {
            let pos = transform.translation.truncate();
            let d = cursor.distance(pos);
            let reach = CLICK_RADIUS.max(interactive.radius - TILE * 1.3);
            if d <= reach && hit.is_none_or(|(best, _, _)| d < best) {
                hit = Some((d, pos, interactive));
            }
        }
        ui.cancel_travel();
        match hit {
            Some((_, pos, interactive)) if player_pos.distance(pos) <= interactive.radius => {
                let menu = target_menu(&world, interactive);
                ui.open_menu(menu, screen_cursor);
            }
            Some((_, pos, interactive)) => {
                ui.approach = Some(interactive.name.clone());
                if !walk_toward(&grid, player_pos, pos, interactive.radius, &mut ui) {
                    ui.approach = None;
                    ui.toast(format!("Can't find a way to {}.", interactive.name));
                }
            }
            None => {
                if !walk_toward(&grid, player_pos, cursor, TILE * 0.5, &mut ui) {
                    ui.toast("Can't walk there.");
                }
            }
        }
        return;
    }
    if right && ui.travelling() {
        ui.cancel_travel();
        return;
    }

    // Walking up to someone: open their menu once in reach; keep re-aiming if they move.
    if let Some(name) = ui.approach.clone() {
        let target = targets.iter().find(|(_, i)| i.name == name);
        match target {
            None => ui.cancel_travel(),
            Some((transform, interactive)) => {
                let pos = transform.translation.truncate();
                if player_pos.distance(pos) <= interactive.radius {
                    ui.cancel_travel();
                    let anchor = camera
                        .single()
                        .ok()
                        .and_then(|(cam, gt)| cam.world_to_viewport(gt, pos.extend(0.0)).ok());
                    let menu = target_menu(&world, interactive);
                    ui.open_menu(menu, anchor);
                } else if (ui.auto_walk.is_empty()
                    || ui.approach_path_at.is_none_or(|t| t.elapsed() >= APPROACH_REPATH))
                    && !walk_toward(&grid, player_pos, pos, interactive.radius, &mut ui)
                {
                    ui.cancel_travel();
                    ui.toast(format!("Can't reach {name}."));
                }
            }
        }
    }

    if keys.just_pressed(KeyCode::KeyE) {
        match &nearby.target {
            Some(target) => {
                let menu = target_menu(&world, target);
                ui.open_menu(menu, None);
            }
            None => ui.toast("Click someone or something (or walk up to it and press E)."),
        }
    }
}

/// Plan a walk to within `reach` px of `target`; false if there is no way there.
fn walk_toward(grid: &WalkGrid, from: Vec2, target: Vec2, reach: f32, ui: &mut UiState) -> bool {
    let start = cell_at(from);
    let center = cell_at(target);
    let r = (reach / TILE).ceil() as i32 + 1;
    let limit = reach.max(TILE * 0.5) * 0.9;
    let mut goals: HashSet<IVec2> = (-r..=r)
        .flat_map(|dx| (-r..=r).map(move |dy| center + IVec2::new(dx, dy)))
        .filter(|c| grid.is_open(*c) && cell_center(*c).distance(target) <= limit)
        .collect();
    if goals.is_empty() && grid.is_open(center) {
        goals.insert(center);
    }
    ui.approach_path_at = Some(Instant::now());
    if goals.contains(&start) {
        ui.auto_walk.clear();
        return true;
    }
    match grid.path(start, &goals) {
        Some(path) => {
            ui.auto_walk = path;
            true
        }
        None => false,
    }
}

fn global_keys(keys: &ButtonInput<KeyCode>, ui: &mut UiState, net: &mut NetBridge) {
    let shift = keys.pressed(KeyCode::ShiftLeft) || keys.pressed(KeyCode::ShiftRight);
    if keys.just_pressed(KeyCode::KeyR) {
        net.refresh_now();
    }
    if keys.just_pressed(KeyCode::KeyP) {
        let start = !ui.sim_running;
        net.set_simulation(start, shift);
        ui.toast(match (start, shift) {
            (true, true) => "Starting NPC simulation (LLM decisions)...",
            (true, false) => "Starting NPC simulation (rule-based)...",
            _ => "Pausing NPC simulation...",
        });
    }
    if keys.just_pressed(KeyCode::KeyT) {
        net.advance_time(if shift { 6.0 } else { 1.0 });
    }
}

fn npc_names(world: &WorldSnapshot) -> Vec<String> {
    let mut names: Vec<String> = world
        .characters
        .iter()
        .filter(|c| c.name != world.player.name)
        .map(|c| c.name.clone())
        .collect();
    names.sort();
    names
}

fn history_input(keys: &ButtonInput<KeyCode>, world: &WorldSnapshot, ui: &mut UiState) {
    if keys.just_pressed(KeyCode::Escape) || keys.just_pressed(KeyCode::KeyH) {
        ui.history = None;
        return;
    }
    let Some(view) = ui.history.as_mut() else {
        return;
    };
    let names = npc_names(world);
    let step: isize = if keys.just_pressed(KeyCode::KeyD) || keys.just_pressed(KeyCode::ArrowRight) {
        1
    } else if keys.just_pressed(KeyCode::KeyA) || keys.just_pressed(KeyCode::ArrowLeft) {
        -1
    } else {
        0
    };
    if step != 0 && !names.is_empty() {
        let idx = names.iter().position(|n| *n == view.name).unwrap_or(0) as isize;
        let next = (idx + step).rem_euclid(names.len() as isize) as usize;
        *view = HistoryView::new(names[next].clone());
        return;
    }
    if keys.just_pressed(KeyCode::KeyW) || keys.just_pressed(KeyCode::ArrowUp) {
        view.scroll = view.scroll.saturating_sub(1);
    }
    if keys.just_pressed(KeyCode::KeyS) || keys.just_pressed(KeyCode::ArrowDown) {
        view.scroll += 1;
    }
    if keys.just_pressed(KeyCode::PageUp) {
        view.scroll = view.scroll.saturating_sub(HISTORY_ENTRIES / 2);
    }
    if keys.just_pressed(KeyCode::PageDown) {
        view.scroll += HISTORY_ENTRIES / 2;
    }
    if keys.just_pressed(KeyCode::Home) {
        view.scroll = 0;
    }
}

/// Mouse wheel scrolls the history panel (one entry per notch; wheel up = newer).
pub fn history_wheel(scroll: Res<AccumulatedMouseScroll>, mut ui: ResMut<UiState>) {
    let Some(view) = ui.history.as_mut() else {
        return;
    };
    if scroll.delta.y == 0.0 {
        return;
    }
    let notches = match scroll.unit {
        MouseScrollUnit::Line => scroll.delta.y,
        MouseScrollUnit::Pixel => scroll.delta.y / 40.0,
    };
    view.wheel -= notches;
    let whole = view.wheel.trunc();
    if whole != 0.0 {
        view.wheel -= whole;
        view.scroll = (view.scroll as isize + whole as isize).max(0) as usize;
    }
}

/// Re-fetch the open history panel about once a second.
pub fn poll_history(net: Res<NetBridge>, mut ui: ResMut<UiState>) {
    let Some(view) = ui.history.as_mut() else {
        return;
    };
    if view.in_flight || view.last_fetch.is_some_and(|t| t.elapsed() < HISTORY_POLL) {
        return;
    }
    view.in_flight = true;
    view.last_fetch = Some(Instant::now());
    net.fetch_history(&view.name);
}

fn menu_input(keys: &ButtonInput<KeyCode>, world: &WorldSnapshot, ui: &mut UiState, net: &NetBridge) {
    let Some(menu) = ui.menu.as_mut() else {
        return;
    };
    let count = menu.options.len().max(1);
    if keys.just_pressed(KeyCode::KeyW) || keys.just_pressed(KeyCode::ArrowUp) {
        menu.selected = (menu.selected + count - 1) % count;
    }
    if keys.just_pressed(KeyCode::KeyS) || keys.just_pressed(KeyCode::ArrowDown) {
        menu.selected = (menu.selected + 1) % count;
    }

    let digits = [
        KeyCode::Digit1,
        KeyCode::Digit2,
        KeyCode::Digit3,
        KeyCode::Digit4,
        KeyCode::Digit5,
        KeyCode::Digit6,
        KeyCode::Digit7,
        KeyCode::Digit8,
        KeyCode::Digit9,
    ];
    let mut confirm = [KeyCode::KeyE, KeyCode::Enter, KeyCode::Space]
        .iter()
        .any(|k| keys.just_pressed(*k));
    for (i, key) in digits.iter().enumerate() {
        if keys.just_pressed(*key) && i < menu.options.len() {
            menu.selected = i;
            confirm = true;
        }
    }

    if keys.just_pressed(KeyCode::Escape) || keys.just_pressed(KeyCode::Backspace) {
        ui.menu = menu.parent.take().map(|parent| *parent);
        return;
    }
    if keys.just_pressed(KeyCode::KeyI) || keys.just_pressed(KeyCode::Tab) {
        ui.menu = None;
        return;
    }
    if !confirm || menu.options.is_empty() {
        return;
    }
    let selected = menu.selected;
    choose_menu_option(selected, world, ui, net);
}

fn choose_menu_option(index: usize, world: &WorldSnapshot, ui: &mut UiState, net: &NetBridge) {
    let Some(menu) = ui.menu.as_mut() else {
        return;
    };
    if menu.options.is_empty() {
        return;
    }
    let selected = index.min(menu.options.len() - 1);
    menu.selected = selected;
    let command = menu.options[selected].command.clone();
    match command {
        Command::Close => ui.menu = None,
        Command::Back => ui.menu = menu.parent.take().map(|parent| *parent),
        Command::Submenu(mut sub) => {
            sub.parent = ui.menu.take().map(Box::new);
            ui.menu = Some(*sub);
        }
        Command::Examine { title, text } => {
            ui.menu = None;
            ui.dialogue = Some(DialogueView::shown(title, vec![(String::new(), text)]));
        }
        Command::History { person } => {
            ui.menu = None;
            ui.history = Some(HistoryView::new(person));
        }
        Command::StartBuild(placement) => {
            ui.menu = None;
            ui.toast(format!(
                "Placing {}: move the mouse to place the outline, click to build, right-click to cancel.",
                placement.label
            ));
            ui.build = Some(placement);
        }
        Command::Talk { person } => {
            ui.menu = None;
            if !ensure_online(world, ui) {
                return;
            }
            // Open the chat box; nothing is said until the player types something.
            ui.dialogue = Some(DialogueView::chat(person));
        }
        Command::Act { action, params } => {
            ui.menu = None;
            if !ensure_online(world, ui) {
                return;
            }
            let params = params
                .into_iter()
                .map(|(k, v)| (k.to_string(), v))
                .collect::<HashMap<_, _>>();
            net.execute(action, params);
        }
    }
}

fn ensure_online(world: &WorldSnapshot, ui: &mut UiState) -> bool {
    if world.online && world.found {
        return true;
    }
    ui.toast("Not connected to a live world yet; press R after the server is ready.");
    false
}

fn cursor_world(
    windows: &Query<&Window>,
    camera: &Query<(&Camera, &GlobalTransform)>,
) -> Option<Vec2> {
    let window = windows.single().ok()?;
    let cursor = window.cursor_position()?;
    let (camera, transform) = camera.single().ok()?;
    camera.viewport_to_world_2d(transform, cursor).ok()
}

// ---------------------------------------------------------------------------
// Widgets
// ---------------------------------------------------------------------------

#[derive(Component)]
pub struct DebugHud;

#[derive(Component)]
pub struct StatusText;

#[derive(Component)]
pub struct PromptText;

#[derive(Component)]
pub struct ToastText;

#[derive(Component)]
pub struct MenuPanel;

#[derive(Component)]
pub struct DialoguePanel;

#[derive(Component)]
pub struct NightOverlay;

#[derive(Component)]
pub struct ClockText;

#[derive(Component)]
pub struct HistoryPanel;

pub fn setup_ui(mut commands: Commands) {
    commands.spawn((
        Node {
            position_type: PositionType::Absolute,
            width: percent(100),
            height: percent(100),
            ..default()
        },
        BackgroundColor(Color::NONE),
        GlobalZIndex(-10),
        NightOverlay,
    ));
    commands.spawn((
        Text::new(""),
        TextFont {
            font_size: 14.0,
            ..default()
        },
        Node {
            position_type: PositionType::Absolute,
            top: px(14),
            left: px(16),
            max_width: px(560),
            padding: UiRect::all(px(8)),
            ..default()
        },
        BackgroundColor(Color::srgba(0.0, 0.0, 0.0, 0.7)),
        Visibility::Hidden,
        DebugHud,
    ));
    commands.spawn((
        Text::new(""),
        TextFont {
            font_size: 16.0,
            ..default()
        },
        Node {
            position_type: PositionType::Absolute,
            top: px(14),
            left: px(16),
            ..default()
        },
        StatusText,
    ));
    commands.spawn((
        Text::new(""),
        TextFont {
            font_size: 26.0,
            ..default()
        },
        TextColor(Color::srgb(0.98, 0.92, 0.70)),
        TextLayout::new_with_justify(Justify::Right),
        Node {
            position_type: PositionType::Absolute,
            top: px(10),
            right: px(16),
            padding: UiRect::axes(px(12), px(6)),
            ..default()
        },
        BackgroundColor(Color::srgba(0.0, 0.0, 0.0, 0.55)),
        ClockText,
    ));
    commands.spawn((
        Text::new(""),
        TextFont {
            font_size: 18.0,
            ..default()
        },
        TextColor(Color::srgb(0.98, 0.85, 0.40)),
        TextLayout::new_with_justify(Justify::Center),
        Node {
            position_type: PositionType::Absolute,
            bottom: px(200),
            width: percent(100),
            justify_content: JustifyContent::Center,
            ..default()
        },
        PromptText,
    ));
    commands.spawn((
        Text::new(""),
        TextFont {
            font_size: 16.0,
            ..default()
        },
        Node {
            position_type: PositionType::Absolute,
            top: px(100),
            right: px(16),
            max_width: px(460),
            padding: UiRect::axes(px(12), px(8)),
            ..default()
        },
        BackgroundColor(Color::srgba(0.05, 0.07, 0.09, 0.85)),
        Visibility::Hidden,
        ToastText,
    ));
    commands.spawn((
        Node {
            position_type: PositionType::Absolute,
            top: percent(22),
            right: percent(8),
            width: px(MENU_WIDTH),
            flex_direction: FlexDirection::Column,
            padding: UiRect::all(px(10)),
            border: UiRect::all(px(2)),
            ..default()
        },
        BackgroundColor(Color::srgba(0.06, 0.08, 0.11, 0.96)),
        BorderColor::all(Color::srgb(0.55, 0.70, 0.85)),
        Interaction::default(),
        FocusPolicy::Block,
        GlobalZIndex(10),
        Visibility::Hidden,
        MenuPanel,
    ));
    commands.spawn((
        Text::new(""),
        TextFont {
            font_size: 18.0,
            ..default()
        },
        TextColor(Color::srgb(0.94, 0.95, 0.92)),
        Node {
            position_type: PositionType::Absolute,
            bottom: px(20),
            left: percent(10),
            width: percent(80),
            min_height: px(150),
            padding: UiRect::all(px(16)),
            border: UiRect::all(px(2)),
            ..default()
        },
        BackgroundColor(Color::srgba(0.06, 0.08, 0.11, 0.92)),
        BorderColor::all(Color::srgb(0.85, 0.68, 0.30)),
        Button,
        UiButton(UiAction::CloseDialogue),
        GlobalZIndex(8),
        Visibility::Hidden,
        DialoguePanel,
    ));
    commands.spawn((
        Node {
            position_type: PositionType::Absolute,
            top: px(70),
            left: percent(5),
            width: percent(70),
            bottom: px(64),
            flex_direction: FlexDirection::Column,
            row_gap: px(2),
            padding: UiRect::all(px(12)),
            border: UiRect::all(px(2)),
            overflow: Overflow::clip(),
            ..default()
        },
        BackgroundColor(Color::srgba(0.04, 0.05, 0.08, 0.96)),
        BorderColor::all(Color::srgb(0.45, 0.75, 0.65)),
        Interaction::default(),
        FocusPolicy::Block,
        GlobalZIndex(5),
        Visibility::Hidden,
        HistoryPanel,
    ));
    commands.spawn((
        Node {
            position_type: PositionType::Absolute,
            bottom: px(10),
            left: px(16),
            flex_direction: FlexDirection::Row,
            column_gap: px(6),
            ..default()
        },
        GlobalZIndex(6),
        Toolbar,
    ));
}

fn short_clock(clock: &str) -> &str {
    clock.split(" (").next().unwrap_or(clock)
}

fn set_text(text: &mut Text, value: String) {
    if text.0 != value {
        text.0 = value;
    }
}

fn set_visible(vis: &mut Visibility, visible: bool) {
    let next = if visible {
        Visibility::Visible
    } else {
        Visibility::Hidden
    };
    if *vis != next {
        *vis = next;
    }
}

/// Darkness by hour of day: full night 22–4, fading through dusk and dawn.
fn darkness(hour: f32) -> f32 {
    let h = hour.rem_euclid(24.0);
    let night = 0.55;
    match h {
        h if (4.0..7.0).contains(&h) => night * (7.0 - h) / 3.0,
        h if (7.0..18.0).contains(&h) => 0.0,
        h if (18.0..22.0).contains(&h) => night * (h - 18.0) / 4.0,
        _ => night,
    }
}

pub fn update_night_overlay(
    world: Res<WorldSnapshot>,
    mut query: Query<&mut BackgroundColor, With<NightOverlay>>,
) {
    let Ok(mut bg) = query.single_mut() else {
        return;
    };
    let alpha = if world.online && !world.simulation.clock.text.is_empty() {
        darkness(world.simulation.clock.hour)
    } else {
        0.0
    };
    let next = Color::srgba(0.02, 0.03, 0.12, alpha);
    if bg.0 != next {
        bg.0 = next;
    }
}

pub fn update_status(
    world: Res<WorldSnapshot>,
    ui: Res<UiState>,
    net: Res<NetBridge>,
    mut query: Query<&mut Text, With<StatusText>>,
) {
    let Ok(mut text) = query.single_mut() else {
        return;
    };
    let value = if ui.debug_visible {
        String::new()
    } else if !world.online {
        trim_line(&net.last_message, 140)
    } else {
        format!(
            "{}  ·  {}  ·  HP {}   ·   NPCs {}",
            world.player.name,
            world.current_location.name,
            world.player.hp,
            if ui.sim_running { "running" } else { "paused" },
        )
    };
    set_text(&mut text, value);
}

pub fn update_clock(
    world: Res<WorldSnapshot>,
    ui: Res<UiState>,
    mut query: Query<(&mut Text, &mut Visibility), With<ClockText>>,
) {
    let Ok((mut text, mut vis)) = query.single_mut() else {
        return;
    };
    let clock = &world.simulation.clock;
    let shown = world.online && !clock.text.is_empty();
    set_visible(&mut vis, shown);
    if !shown {
        return;
    }
    let total_minutes = (clock.hour.rem_euclid(24.0) * 60.0).floor() as u32;
    let state = if ui.sim_running { "" } else { "  (paused)" };
    let arc = &world.simulation.story.arc;
    let mut story = if arc.is_empty() {
        String::new()
    } else {
        format!("\nStory: {}", trim_line(arc, 90))
    };
    if let Some(quest) = world.current_objective() {
        story.push_str(&format!("\nQuest - {}: {}", quest.stage, trim_line(&quest.objective, 90)));
    }
    set_text(
        &mut text,
        format!(
            "Day {}  {:02}:{:02}\n{}{}{}",
            clock.day.max(1),
            total_minutes / 60,
            total_minutes % 60,
            clock.phase_label,
            state,
            story,
        ),
    );
}

pub fn update_prompt(
    nearby: Res<NearbyInteract>,
    ui: Res<UiState>,
    world: Res<WorldSnapshot>,
    mut query: Query<&mut Text, With<PromptText>>,
) {
    let Ok(mut text) = query.single_mut() else {
        return;
    };
    let value = match (&ui.build, &nearby.target) {
        (Some(build), _) => format!("Placing {}   click to build   right-click to cancel", build.label),
        _ if ui.approach.is_some() => format!("Walking to {}...", ui.approach.clone().unwrap_or_default()),
        (None, Some(target)) if !ui.blocks_movement() => {
            let mut line = format!("{}  (click)", target.prompt);
            // Standing next to an NPC: show what they are after and why.
            if let TargetKind::Person { name } = &target.kind {
                if let Some(activity) = world.activity(name) {
                    let goal = thought_line(activity);
                    if !goal.is_empty() {
                        line.push_str(&format!("\n{name} wants: {goal}"));
                    }
                    if !activity.reason.is_empty() {
                        line.push_str(&format!("\n\"{}\"", trim_line(&activity.reason, 110)));
                    }
                }
            }
            line
        }
        _ if ui.travelling() => format!(
            "Walking to {}...   (right-click to stop)",
            ui.travel_to.clone().unwrap_or_default()
        ),
        _ => String::new(),
    };
    set_text(&mut text, value);
}

pub fn update_toast(ui: Res<UiState>, mut query: Query<(&mut Text, &mut Visibility), With<ToastText>>) {
    let Ok((mut text, mut vis)) = query.single_mut() else {
        return;
    };
    let show = !ui.toast.is_empty() && ui.toast_at.elapsed() < TOAST_DURATION;
    set_visible(&mut vis, show);
    if show {
        set_text(&mut text, ui.toast.clone());
    }
}

pub fn update_dialogue_panel(
    ui: Res<UiState>,
    world: Res<WorldSnapshot>,
    mut query: Query<(&mut Text, &mut Visibility), With<DialoguePanel>>,
) {
    let me = world.player.name.clone();
    let Ok((mut text, mut vis)) = query.single_mut() else {
        return;
    };
    let Some(view) = &ui.dialogue else {
        set_visible(&mut vis, false);
        return;
    };
    set_visible(&mut vis, true);
    let mut body = if view.partner.is_empty() {
        String::new()
    } else {
        format!("— {} —\n\n", view.partner)
    };
    if view.chat {
        let shown = view.lines.len().saturating_sub(CHAT_SHOWN);
        for (speaker, line) in &view.lines[shown..] {
            if speaker.is_empty() {
                body.push_str(&format!("{line}\n"));
            } else {
                let who = if *speaker == me { "You" } else { speaker.as_str() };
                body.push_str(&format!("{who}: {line}\n"));
            }
        }
        if view.lines.is_empty() {
            body.push_str("(Type what you want to say, then press Enter.)\n");
        }
        if view.waiting {
            body.push_str(&format!("{} is thinking...\n", view.partner));
        }
        let caret = if (ui.toast_at.elapsed().as_millis() / 500) % 2 == 0 { "|" } else { " " };
        body.push_str(&format!("\n> {}{}{caret}\n\n[Enter] say   [Esc / right-click] leave", view.input, view.preedit));
    } else if view.waiting {
        body.push_str("...");
    } else {
        for (speaker, line) in &view.lines {
            if speaker.is_empty() {
                body.push_str(line);
            } else {
                body.push_str(&format!("{speaker}: {line}"));
            }
            body.push_str("\n\n");
        }
        body.push_str("(click to close)");
    }
    set_text(&mut text, body);
}

pub fn update_debug_hud(
    world: Res<WorldSnapshot>,
    net: Res<NetBridge>,
    ui: Res<UiState>,
    mut query: Query<(&mut Text, &mut Visibility), With<DebugHud>>,
) {
    let Ok((mut text, mut vis)) = query.single_mut() else {
        return;
    };
    set_visible(&mut vis, ui.debug_visible);
    if !ui.debug_visible {
        return;
    }
    let people = world
        .people_here
        .iter()
        .map(|p| format!("{} ({}, HP {})", p.name, p.role, p.hp))
        .collect::<Vec<_>>()
        .join(", ");
    let items = world
        .items_here
        .iter()
        .map(|i| i.name.clone())
        .collect::<Vec<_>>()
        .join(", ");
    let activity = world
        .simulation
        .npc_activity
        .iter()
        .map(|a| {
            let doing = if !a.state.is_empty() {
                a.state.clone()
            } else if !a.now_label.is_empty() {
                format!("{} [{}/{}]", a.now_label, a.plan_step, a.plan_len)
            } else if a.pending_action.is_empty() {
                a.status.clone()
            } else {
                format!("{}({})", a.pending_action, a.pending_args.join(", "))
            };
            let mut line = if a.reason.is_empty() {
                format!("  {}: {doing}", a.name)
            } else {
                format!("  {}: {doing} — {}", a.name, trim_line(&a.reason, 60))
            };
            if !a.trigger.is_empty() {
                line.push_str(&format!("\n      because: {}", trim_line(&a.trigger, 70)));
            }
            line
        })
        .collect::<Vec<_>>()
        .join("\n");
    let news = world
        .simulation
        .chronicle
        .iter()
        .take(4)
        .map(|e| format!("  {}: {}", e.clock, trim_line(&e.text, 80)))
        .collect::<Vec<_>>()
        .join("\n");
    let value = format!(
        "AITown3 [{mode}]  sim {sim}  tick {tick}  {clock}\n{player} @ {loc}  HP {hp}\n{desc}\n\nPeople: {people}\nItems: {items}\nBag: {bag}\nExits: {exits}\n\nNPC activity:\n{activity}\n\nVillage news:\n{news}\n\nClick: walk / interact | Right-click: cancel | WASD walk | E interact | I bag | P sim (Shift+P: LLM) | T +1h (Shift+T: +6h) | R refresh | ~ debug\n{message}",
        mode = if world.online { "online" } else { "offline" },
        sim = if ui.sim_running { "running" } else { "paused" },
        tick = world.simulation.tick,
        clock = world.simulation.clock.text,
        player = world.player.name,
        loc = world.current_location.name,
        hp = world.player.hp,
        desc = trim_line(&world.current_location.description, 100),
        bag = world.held_items().join(", "),
        exits = world.adjacent.join(", "),
        activity = if activity.is_empty() { "  (none)".to_string() } else { activity },
        news = if news.is_empty() { "  (none)".to_string() } else { news },
        message = net.last_message,
    );
    set_text(&mut text, value);
}

// ---------------------------------------------------------------------------
// Mouse UI: buttons, toolbar, click-to-expand history
// ---------------------------------------------------------------------------

const TEXT: Color = Color::srgb(0.94, 0.95, 0.92);
const MUTED: Color = Color::srgb(0.66, 0.72, 0.74);
const TITLE: Color = Color::srgb(0.98, 0.88, 0.60);
const SAID: Color = Color::srgb(0.98, 0.86, 0.55);

#[derive(Clone, PartialEq, Debug)]
pub enum UiAction {
    MenuOption(usize),
    Bag,
    Build,
    Map,
    History,
    /// Start (rule / LLM decisions) or pause the NPC simulation.
    Sim { llm: bool },
    AdvanceTime,
    Refresh,
    ToggleEntry(String),
    HistoryStep(isize),
    CloseHistory,
    CloseDialogue,
}

#[derive(Component, Clone)]
pub struct UiButton(pub UiAction);

#[derive(Component)]
pub struct ButtonColors {
    base: Color,
    hover: Color,
}

#[derive(Component)]
pub struct Toolbar;

fn button(p: &mut ChildSpawnerCommands, label: impl Into<String>, action: UiAction, font: f32, highlighted: bool, wide: bool) {
    let base = if highlighted { Color::srgb(0.20, 0.28, 0.38) } else { Color::srgb(0.11, 0.14, 0.18) };
    p.spawn((
        Button,
        UiButton(action),
        ButtonColors { base, hover: Color::srgb(0.28, 0.40, 0.54) },
        Node {
            padding: UiRect::axes(px(10), px(6)),
            margin: UiRect::vertical(px(2)),
            width: if wide { percent(100) } else { Val::Auto },
            ..default()
        },
        BackgroundColor(base),
    ))
    .with_children(|b| {
        b.spawn((Text::new(label), TextFont { font_size: font, ..default() }, TextColor(TEXT)));
    });
}

pub fn button_hover(mut buttons: Query<(&Interaction, &ButtonColors, &mut BackgroundColor), Changed<Interaction>>) {
    for (interaction, colors, mut bg) in &mut buttons {
        bg.0 = if *interaction == Interaction::None { colors.base } else { colors.hover };
    }
}

pub fn ui_buttons(
    pressed: Query<(&Interaction, &UiButton), Changed<Interaction>>,
    world: Res<WorldSnapshot>,
    nearby: Res<NearbyInteract>,
    mut ui: ResMut<UiState>,
    mut net: ResMut<NetBridge>,
) {
    let actions: Vec<UiAction> = pressed
        .iter()
        .filter(|(i, _)| **i == Interaction::Pressed)
        .map(|(_, b)| b.0.clone())
        .collect();
    for action in actions {
        run_ui_action(action, &world, &nearby, &mut ui, &mut net);
    }
}

fn run_ui_action(action: UiAction, world: &WorldSnapshot, nearby: &NearbyInteract, ui: &mut UiState, net: &mut NetBridge) {
    match action {
        UiAction::MenuOption(i) => choose_menu_option(i, world, ui, net),
        UiAction::Bag => {
            if ui.menu.as_ref().is_some_and(|m| m.title == "Inventory") {
                ui.menu = None;
            } else {
                let menu = inventory_menu(world);
                ui.open_menu(menu, None);
            }
        }
        UiAction::Build => {
            if ensure_online(world, ui) {
                net.fetch_blueprints();
            }
        }
        UiAction::Map => {
            ui.menu = None;
            ui.history = None;
            ui.world_map = match ui.world_map {
                Some(_) => None,
                None => Some(WorldMapView { selected: world.current_location.name.clone() }),
            };
        }
        UiAction::History => {
            ui.menu = None;
            ui.world_map = None;
            if ui.history.is_some() {
                ui.history = None;
                return;
            }
            let near_person = match nearby.target.as_ref().map(|t| &t.kind) {
                Some(TargetKind::Person { name }) => Some(name.clone()),
                _ => None,
            };
            match near_person.or_else(|| npc_names(world).into_iter().next()) {
                Some(name) => ui.history = Some(HistoryView::new(name)),
                None => ui.toast("No NPCs to inspect."),
            }
        }
        UiAction::Sim { llm } => {
            let start = !ui.sim_running;
            net.set_simulation(start, llm);
            ui.toast(match (start, llm) {
                (true, true) => "Starting NPC simulation (LLM decisions)...",
                (true, false) => "Starting NPC simulation (rule-based)...",
                _ => "Pausing NPC simulation...",
            });
        }
        UiAction::AdvanceTime => net.advance_time(1.0),
        UiAction::Refresh => net.refresh_now(),
        UiAction::ToggleEntry(key) => {
            if let Some(view) = ui.history.as_mut() {
                if !view.expanded.remove(&key) {
                    view.expanded.insert(key);
                }
            }
        }
        UiAction::HistoryStep(step) => {
            let names = npc_names(world);
            if let (Some(view), false) = (ui.history.as_mut(), names.is_empty()) {
                let idx = names.iter().position(|n| *n == view.name).unwrap_or(0) as isize;
                let next = (idx + step).rem_euclid(names.len() as isize) as usize;
                *view = HistoryView::new(names[next].clone());
            }
        }
        UiAction::CloseHistory => ui.history = None,
        UiAction::CloseDialogue => {
            if ui.dialogue.as_ref().is_some_and(|d| !d.waiting && !d.chat) {
                ui.dialogue = None;
            }
        }
    }
}

/// Bottom bar: everything the keyboard shortcuts do, as buttons.
pub fn update_toolbar(
    mut commands: Commands,
    ui: Res<UiState>,
    world: Res<WorldSnapshot>,
    bar: Query<Entity, With<Toolbar>>,
    mut shown: Local<String>,
) {
    let Ok(bar) = bar.single() else { return };
    let bag = world.held_items().len();
    let signature = format!("{}|{}|{bag}", world.online, ui.sim_running);
    if *shown == signature {
        return;
    }
    *shown = signature;
    commands.entity(bar).despawn_related::<Children>();
    commands.entity(bar).with_children(|p| {
        if !world.online {
            button(p, "Reconnect", UiAction::Refresh, 15.0, false, false);
            return;
        }
        button(p, format!("Bag ({bag})"), UiAction::Bag, 15.0, false, false);
        button(p, "Build", UiAction::Build, 15.0, false, false);
        button(p, "Map", UiAction::Map, 15.0, false, false);
        button(p, "History", UiAction::History, 15.0, false, false);
        if ui.sim_running {
            button(p, "Pause NPCs", UiAction::Sim { llm: false }, 15.0, true, false);
        } else {
            button(p, "Run NPCs (rules)", UiAction::Sim { llm: false }, 15.0, false, false);
            button(p, "Run NPCs (LLM)", UiAction::Sim { llm: true }, 15.0, false, false);
        }
        button(p, "+1 hour", UiAction::AdvanceTime, 15.0, false, false);
    });
}

pub fn update_menu_panel(
    mut commands: Commands,
    ui: Res<UiState>,
    windows: Query<&Window>,
    mut panel: Query<(Entity, &mut Node, &mut Visibility), With<MenuPanel>>,
    mut shown: Local<String>,
) {
    let Ok((entity, mut node, mut vis)) = panel.single_mut() else {
        return;
    };
    let Some(menu) = &ui.menu else {
        set_visible(&mut vis, false);
        if !shown.is_empty() {
            shown.clear();
            commands.entity(entity).despawn_related::<Children>();
        }
        return;
    };
    set_visible(&mut vis, true);
    let labels: Vec<&str> = menu.options.iter().map(|o| o.label.as_str()).collect();
    let signature = format!("{}|{}|{:?}|{}|{}", menu.title, menu.subtitle, ui.menu_anchor, menu.selected, labels.join("|"));
    if *shown == signature {
        return;
    }
    *shown = signature;

    let window = windows.single().ok().map(|w| Vec2::new(w.width(), w.height()));
    match (ui.menu_anchor, window) {
        (Some(at), Some(size)) => {
            let height = 80.0 + menu.options.len() as f32 * 34.0;
            node.left = px((at.x + 18.0).min(size.x - MENU_WIDTH - 8.0).max(8.0));
            node.top = px((at.y - 24.0).min(size.y - height - 8.0).max(8.0));
            node.right = Val::Auto;
        }
        _ => {
            node.left = Val::Auto;
            node.right = percent(8);
            node.top = percent(22);
        }
    }
    commands.entity(entity).despawn_related::<Children>();
    commands.entity(entity).with_children(|p| {
        p.spawn((Text::new(menu.title.clone()), TextFont { font_size: 19.0, ..default() }, TextColor(TITLE)));
        if !menu.subtitle.is_empty() {
            p.spawn((
                Text::new(menu.subtitle.clone()),
                TextFont { font_size: 14.0, ..default() },
                TextColor(MUTED),
            ));
        }
        p.spawn(Node { height: px(6), ..default() });
        for (i, option) in menu.options.iter().enumerate() {
            button(p, option.label.clone(), UiAction::MenuOption(i), 16.0, i == menu.selected, true);
        }
    });
}

fn entry_key(e: &HistoryEntry) -> String {
    format!("{}|{}|{}", e.clock, e.kind, e.text)
}

/// One plain sentence for the list.
fn entry_summary(e: &HistoryEntry) -> String {
    let text = if e.line.is_empty() { e.text.as_str() } else { e.line.as_str() };
    format!("{}   {}", short_clock(&e.clock), trim_line(text, 140))
}

/// What opens up when the entry is clicked: what was said, why, the plan, the raw step.
fn entry_details(e: &HistoryEntry) -> (Vec<String>, Vec<String>) {
    let said: Vec<String> = e.dialogue.iter().map(|d| d.to_string()).collect();
    let mut more = Vec::new();
    for t in &e.triggers {
        more.push(format!("because: {t}"));
    }
    if !e.reason.is_empty() && e.reason != e.why {
        let label = if e.kind == "decision" { "thinking" } else { "why" };
        more.push(format!("{label}: {}", e.reason));
    }
    if !e.message.is_empty() && e.kind != "done" {
        more.push(e.message.clone());
    }
    if !e.plan.is_empty() {
        more.push(format!("plan: {}", e.plan.join(" -> ")));
    }
    if !e.note.is_empty() {
        more.push(format!("({})", e.note));
    }
    let tags: Vec<&str> = [e.source.as_str(), e.mode.as_str(), e.step.as_str()]
        .into_iter()
        .filter(|s| !s.is_empty())
        .collect();
    let tags = if tags.is_empty() { String::new() } else { format!("  ({})", tags.join(", ")) };
    more.push(format!("[{}] {}{tags}", e.kind, e.text));
    (said, more)
}

/// Entries worth listing: "sets off for X" only while it is still happening (the newest).
fn listed_entries(data: &History) -> Vec<&HistoryEntry> {
    data.entries
        .iter()
        .enumerate()
        .filter(|(i, e)| !(e.kind == "start" && *i > 0))
        .map(|(_, e)| e)
        .collect()
}

pub fn update_history_panel(
    mut commands: Commands,
    mut ui: ResMut<UiState>,
    mut panel: Query<(Entity, &mut Visibility), With<HistoryPanel>>,
    mut shown: Local<String>,
) {
    let Ok((entity, mut vis)) = panel.single_mut() else {
        return;
    };
    let Some(view) = ui.history.as_mut() else {
        set_visible(&mut vis, false);
        if !shown.is_empty() {
            shown.clear();
            commands.entity(entity).despawn_related::<Children>();
        }
        return;
    };
    set_visible(&mut vis, true);
    let entries = view.data.as_ref().map(listed_entries).unwrap_or_default();
    view.scroll = view.scroll.min(entries.len().saturating_sub(1));
    let end = (view.scroll + HISTORY_ENTRIES).min(entries.len());
    let window = &entries[view.scroll.min(end)..end];

    let mut expanded: Vec<&String> = view.expanded.iter().collect();
    expanded.sort();
    let signature = format!(
        "{}|{}|{}|{}|{}|{:?}|{}",
        view.name,
        view.error,
        view.data.as_ref().map(plan_signature).unwrap_or_default(),
        view.scroll,
        entries.len(),
        expanded,
        window.iter().map(|e| entry_key(e)).collect::<Vec<_>>().join("\n"),
    );
    if *shown == signature {
        return;
    }
    *shown = signature;

    let name = view.name.clone();
    let view = &*view;
    commands.entity(entity).despawn_related::<Children>();
    commands.entity(entity).with_children(|p| {
        p.spawn(Node { flex_direction: FlexDirection::Row, align_items: AlignItems::Center, column_gap: px(8), ..default() })
            .with_children(|row| {
                button(row, "<", UiAction::HistoryStep(-1), 16.0, false, false);
                row.spawn((Text::new(name.clone()), TextFont { font_size: 20.0, ..default() }, TextColor(TITLE)));
                button(row, ">", UiAction::HistoryStep(1), 16.0, false, false);
                row.spawn(Node { flex_grow: 1.0, ..default() });
                button(row, "Close", UiAction::CloseHistory, 15.0, false, false);
            });
        p.spawn((
            Text::new("Newest first. Click an entry to see what was said and why; scroll with the wheel."),
            TextFont { font_size: 13.0, ..default() },
            TextColor(MUTED),
        ));
        let Some(data) = view.data.as_ref() else {
            let text = if view.error.is_empty() { "Loading...".to_string() } else { format!("Error: {}", view.error) };
            p.spawn((Text::new(text), TextFont { font_size: 15.0, ..default() }, TextColor(TEXT)));
            return;
        };
        let now = if data.goal_line.is_empty() { "nothing in particular" } else { data.goal_line.as_str() };
        let mut now = format!("Right now: wants to {now}.");
        if !data.goal_reason.is_empty() {
            now.push_str(&format!("\nWhy: {}", trim_line(&data.goal_reason, 200)));
        }
        if data.steps.is_empty() && !data.goal.is_empty() {
            now.push_str(&format!("\nGoal: {}", trim_line(&data.goal, 200)));
        }
        p.spawn((
            Text::new(now),
            TextFont { font_size: 15.0, ..default() },
            TextColor(TEXT),
            Node { margin: UiRect::vertical(px(6)), ..default() },
        ));
        plan_box(p, data);
        if entries.is_empty() {
            p.spawn((
                Text::new("(nothing yet - start the NPCs from the bar below)"),
                TextFont { font_size: 15.0, ..default() },
                TextColor(MUTED),
            ));
        }
        for e in window {
            let key = entry_key(e);
            let open = view.expanded.contains(&key);
            let (said, more) = entry_details(e);
            let mut summary = entry_summary(e);
            if !said.is_empty() {
                summary.push_str(&format!("   [{} said - {}]", said.len(), if open { "hide" } else { "read" }));
            } else {
                summary.push_str(if open { "   [-]" } else { "   [+]" });
            }
            let base = if open { Color::srgb(0.13, 0.17, 0.22) } else { Color::srgba(0.0, 0.0, 0.0, 0.0) };
            p.spawn((
                Button,
                UiButton(UiAction::ToggleEntry(key)),
                ButtonColors { base, hover: Color::srgb(0.17, 0.23, 0.30) },
                Node {
                    flex_direction: FlexDirection::Column,
                    width: percent(100),
                    padding: UiRect::axes(px(8), px(4)),
                    ..default()
                },
                BackgroundColor(base),
            ))
            .with_children(|b| {
                b.spawn((Text::new(summary), TextFont { font_size: 15.0, ..default() }, TextColor(TEXT)));
                let reason = entry_reason_line(e, if open { 600 } else { 150 });
                if !reason.is_empty() {
                    b.spawn((
                        Text::new(format!("      {reason}")),
                        TextFont { font_size: 13.0, ..default() },
                        TextColor(MUTED),
                    ));
                }
                if !open {
                    return;
                }
                for line in said {
                    b.spawn((
                        Text::new(format!("    {line}")),
                        TextFont { font_size: 15.0, ..default() },
                        TextColor(SAID),
                    ));
                }
                b.spawn((
                    Text::new(more.iter().map(|l| format!("    {l}")).collect::<Vec<_>>().join("\n")),
                    TextFont { font_size: 13.0, ..default() },
                    TextColor(MUTED),
                ));
            });
        }
        if end < entries.len() {
            p.spawn((
                Text::new(format!("({} older - scroll down)", entries.len() - end)),
                TextFont { font_size: 13.0, ..default() },
                TextColor(MUTED),
            ));
        }
    });
}

/// Typing into the chat box: letters, IME (Chinese input), Backspace, Enter to say it.
pub fn chat_typing(
    mut keys: MessageReader<bevy::input::keyboard::KeyboardInput>,
    mut ime: MessageReader<bevy::window::Ime>,
    mut windows: Query<&mut Window>,
    world: Res<WorldSnapshot>,
    mut ui: ResMut<UiState>,
    net: Res<NetBridge>,
) {
    use bevy::input::keyboard::Key;
    use bevy::window::Ime;

    let chatting = ui.dialogue.as_ref().is_some_and(|d| d.chat);
    if let Ok(mut window) = windows.single_mut() {
        if window.ime_enabled != chatting {
            window.ime_enabled = chatting;
        }
        if chatting {
            let at = Vec2::new(window.width() * 0.12, window.height() - 60.0);
            if window.ime_position != at {
                window.ime_position = at;
            }
        }
    }
    if !chatting {
        keys.clear();
        ime.clear();
        return;
    }
    let Some(view) = ui.dialogue.as_mut() else { return };
    let mut send = false;
    for event in ime.read() {
        match event {
            Ime::Preedit { value, .. } => view.preedit = value.clone(),
            Ime::Commit { value, .. } => {
                view.preedit.clear();
                view.input.push_str(value);
            }
            _ => {}
        }
    }
    for event in keys.read() {
        if !event.state.is_pressed() || !view.preedit.is_empty() {
            continue;
        }
        match &event.logical_key {
            Key::Enter => send = true,
            Key::Backspace => {
                view.input.pop();
            }
            Key::Escape => {}
            _ => {
                if let Some(text) = &event.text {
                    if text.chars().all(|c| !c.is_control()) {
                        view.input.push_str(text);
                    }
                }
            }
        }
    }
    if view.input.chars().count() > CHAT_MAX_CHARS {
        view.input = view.input.chars().take(CHAT_MAX_CHARS).collect();
    }
    if !send || view.waiting {
        return;
    }
    let message = view.input.trim().to_string();
    if message.is_empty() {
        return;
    }
    let history = view.lines.clone();
    view.lines.push((world.player.name.clone(), message.clone()));
    view.input.clear();
    view.waiting = true;
    let mut params = HashMap::new();
    params.insert("a".to_string(), world.player.name.clone());
    params.insert("b".to_string(), view.partner.clone());
    params.insert("l".to_string(), world.current_location.name.clone());
    net.say(params, &message, &history);
}

/// Everything the plan box shows (rebuild only when it changes; progress in 5% steps).
fn plan_signature(d: &History) -> String {
    let steps: Vec<String> = d.steps.iter().map(|s| format!("{}{}", s.state, s.line)).collect();
    format!(
        "{}|{}|{}|{}|{}|{:?}|{}",
        d.status,
        d.goal_line,
        d.goal_reason,
        d.now.label,
        d.now.phase,
        d.now.progress.map(|p| (p * 20.0) as i32),
        steps.join(";"),
    )
}

fn progress_bar(progress: f32) -> String {
    let filled = (progress.clamp(0.0, 1.0) * 20.0).round() as usize;
    format!("[{}{}] {:>3}%", "#".repeat(filled), "-".repeat(20 - filled), (progress * 100.0).round() as i32)
}

/// "Now: doing X [####----] 40%" and the plan as a checklist.
fn plan_box(p: &mut ChildSpawnerCommands, data: &History) {
    let mut status = format!("Now: {}", if data.now.label.is_empty() { data.status.as_str() } else { data.now.label.as_str() });
    if let Some(progress) = data.now.progress {
        status.push_str(&format!("   {}", progress_bar(progress)));
    }
    p.spawn(Node {
        flex_direction: FlexDirection::Column,
        padding: UiRect::all(px(8)),
        margin: UiRect::bottom(px(6)),
        ..default()
    })
    .insert(BackgroundColor(Color::srgba(0.10, 0.14, 0.18, 0.9)))
    .with_children(|b| {
        b.spawn((Text::new(status), TextFont { font_size: 16.0, ..default() }, TextColor(TITLE)));
        if data.steps.is_empty() {
            b.spawn((Text::new("No plan right now."), TextFont { font_size: 14.0, ..default() }, TextColor(MUTED)));
            return;
        }
        b.spawn((
            Text::new(format!("Plan - step {} of {}:", data.step.max(1), data.steps.len())),
            TextFont { font_size: 14.0, ..default() },
            TextColor(MUTED),
            Node { margin: UiRect::top(px(4)), ..default() },
        ));
        for step in &data.steps {
            let (mark, color) = match step.state.as_str() {
                "done" => ("[x]", MUTED),
                "current" => ("[>]", SAID),
                _ => ("[ ]", TEXT),
            };
            b.spawn((
                Text::new(format!("  {mark} {}", step.line)),
                TextFont { font_size: 15.0, ..default() },
                TextColor(color),
            ))
            .with_child((
                TextSpan::new(format!("    {}", step.text)),
                TextFont { font_size: 12.0, ..default() },
                TextColor(Color::srgba(0.6, 0.65, 0.68, 0.7)),
            ));
        }
    });
}

/// An NPC spoke to the player: open the chat box with what they said, so the player
/// answers in their own words. Closing the box tells the server the NPC can go.
pub fn npc_chat_requests(
    world: Res<WorldSnapshot>,
    net: Res<NetBridge>,
    mut ui: ResMut<UiState>,
    mut seen: Local<HashSet<String>>,
    mut synced: Local<bool>,
    mut chatting_with: Local<Option<String>>,
) {
    let me = world.player.name.clone();
    // Chat box closed (or switched to someone else): release the NPC we were talking to.
    let now = ui.dialogue.as_ref().filter(|d| d.chat).map(|d| d.partner.clone());
    if *chatting_with != now {
        if let Some(npc) = chatting_with.take() {
            if world.online && !me.is_empty() {
                net.chat_end(&me, &npc);
            }
        }
        *chatting_with = now;
    }

    if !world.is_changed() {
        return;
    }
    for convo in &world.simulation.conversations {
        if !seen.insert(convo.id.clone()) || !*synced {
            continue;
        }
        if convo.b != me || convo.a == me {
            continue;
        }
        let lines: Vec<(String, String)> = convo
            .lines
            .iter()
            .filter(|l| !l.text.trim().is_empty())
            .map(|l| (l.speaker.clone(), l.text.clone()))
            .collect();
        if lines.is_empty() {
            continue;
        }
        match ui.dialogue.as_mut() {
            Some(view) if view.chat && view.partner == convo.a => view.lines.extend(lines),
            Some(_) => ui.toast(format!("{} wants to talk to you.", convo.a)),
            None => {
                ui.menu = None;
                ui.history = None;
                ui.world_map = None;
                ui.cancel_travel();
                let mut view = DialogueView::chat(convo.a.clone());
                view.lines = lines;
                ui.dialogue = Some(view);
            }
        }
    }
    if world.online {
        *synced = true;
    }
}

/// "for: get Wood1  ·  why: the fire is dying" under every history entry.
fn entry_reason_line(e: &HistoryEntry, max: usize) -> String {
    let mut parts = Vec::new();
    if !e.for_goal.is_empty() && e.kind != "decision" {
        parts.push(format!("for: {}", e.for_goal));
    }
    if !e.why.is_empty() {
        parts.push(format!("why: {}", e.why));
    }
    trim_line(&parts.join("  ·  "), max)
}
