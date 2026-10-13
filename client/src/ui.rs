use bevy::input::mouse::{AccumulatedMouseScroll, MouseScrollUnit};
use bevy::prelude::*;
use std::{
    collections::HashMap,
    time::{Duration, Instant},
};

use crate::net::{trim_line, Blueprint, History, NetBridge, WorldSnapshot};
use crate::scene::{Interactive, PlayerAvatar, TargetKind, ZoneGrid};

const TOAST_DURATION: Duration = Duration::from_secs(4);
const HISTORY_POLL: Duration = Duration::from_secs(1);
const HISTORY_LINES: usize = 34;

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
    /// Remaining zones on the way to a world-map destination.
    pub travel: Vec<String>,
    pub travel_sent: Option<Instant>,
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
            travel: Vec::new(),
            travel_sent: None,
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
        !self.travel.is_empty()
    }

    pub fn cancel_travel(&mut self) {
        self.travel.clear();
        self.travel_sent = None;
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
        self.menu = Some(menu);
    }
}

pub struct DialogueView {
    pub partner: String,
    pub lines: Vec<(String, String)>,
    pub waiting: bool,
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
        TargetKind::Exit { to } => {
            let mut menu = Menu::new(format!("Exit to {to}"), "A path out of here.");
            menu.add(format!("Go to {to}"), act("move", &[("c", me), ("from", here), ("to", to)]));
            menu.add("Leave", Command::Close);
            menu
        }
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
            if !thing.locked && (thing.is_fixture() || !thing.portable) {
                menu.add(
                    format!("Use {name}"),
                    act("interact", &[("c", me), ("target", name), ("option", "auto")]),
                );
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
    player: Query<&Transform, With<PlayerAvatar>>,
    targets: Query<(&Transform, &Interactive), Without<PlayerAvatar>>,
    mut ui: ResMut<UiState>,
    mut net: ResMut<NetBridge>,
) {
    if keys.just_pressed(KeyCode::Backquote) {
        ui.debug_visible = !ui.debug_visible;
    }

    if ui.dialogue.is_some() {
        let close = [KeyCode::Escape, KeyCode::Space, KeyCode::KeyE, KeyCode::Enter]
            .iter()
            .any(|k| keys.just_pressed(*k));
        if close && (keys.just_pressed(KeyCode::Escape) || ui.dialogue.as_ref().is_some_and(|d| !d.waiting)) {
            ui.dialogue = None;
        }
        return;
    }

    if ui.menu.is_some() {
        menu_input(&keys, &world, &mut ui, &net);
        return;
    }

    if ui.history.is_some() {
        history_input(&keys, &world, &mut ui);
        global_keys(&keys, &mut ui, &mut net);
        return;
    }

    if ui.world_map.is_some() {
        crate::world_map::map_input(&keys, &world, &mut ui);
        global_keys(&keys, &mut ui, &mut net);
        return;
    }

    if ui.build.is_some() {
        global_keys(&keys, &mut ui, &mut net);
        if keys.just_pressed(KeyCode::Escape) || keys.just_pressed(KeyCode::KeyB) {
            ui.build = None;
            ui.toast("Building cancelled.");
        } else if keys.just_pressed(KeyCode::KeyE) || keys.just_pressed(KeyCode::Enter) {
            let (Some(build), Ok(player_tf)) = (ui.build.clone(), player.single()) else {
                return;
            };
            let grid = ZoneGrid::of(&world);
            let cell = grid.build_cell(player_tf.translation.truncate(), build.footprint());
            if !grid.can_place(&world, cell, build.footprint()) {
                ui.toast("Can't build there - the outline must be red-free (inside the zone, not on another building).");
            } else if ensure_online(&world, &mut ui) {
                net.build(&build.archetype, cell.x, cell.y);
            }
        }
        return;
    }

    global_keys(&keys, &mut ui, &mut net);
    if keys.just_pressed(KeyCode::KeyB) {
        if ensure_online(&world, &mut ui) {
            net.fetch_blueprints();
        }
        return;
    }
    if keys.just_pressed(KeyCode::KeyM) {
        ui.world_map = Some(WorldMapView {
            selected: world.current_location.name.clone(),
        });
        return;
    }
    if keys.just_pressed(KeyCode::KeyH) {
        let near_person = match nearby.target.as_ref().map(|t| &t.kind) {
            Some(TargetKind::Person { name }) => Some(name.clone()),
            _ => None,
        };
        match near_person.or_else(|| npc_names(&world).into_iter().next()) {
            Some(name) => ui.history = Some(HistoryView::new(name)),
            None => ui.toast("No NPCs to inspect."),
        }
        return;
    }
    if keys.just_pressed(KeyCode::KeyI) || keys.just_pressed(KeyCode::Tab) {
        ui.menu = Some(inventory_menu(&world));
        return;
    }

    let clicked = mouse.just_pressed(MouseButton::Left);
    if clicked {
        let Some(cursor) = cursor_world(&windows, &camera) else {
            return;
        };
        let Ok(player_tf) = player.single() else {
            return;
        };
        let player_pos = player_tf.translation.truncate();
        let mut hit: Option<(f32, &Interactive, bool)> = None;
        for (transform, interactive) in &targets {
            let pos = transform.translation.truncate();
            let cursor_dist = cursor.distance(pos);
            if cursor_dist > 56.0 || hit.is_some_and(|(d, _, _)| cursor_dist >= d) {
                continue;
            }
            let in_range = player_pos.distance(pos) <= interactive.radius;
            hit = Some((cursor_dist, interactive, in_range));
        }
        match hit {
            Some((_, interactive, true)) => ui.menu = Some(target_menu(&world, interactive)),
            Some((_, interactive, false)) => ui.toast(format!("Move closer to {}.", interactive.name)),
            None => {}
        }
        return;
    }

    if keys.just_pressed(KeyCode::KeyE) {
        match &nearby.target {
            Some(target) => ui.menu = Some(target_menu(&world, target)),
            None => ui.toast("Walk up to someone or something, then press E."),
        }
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
        view.scroll = view.scroll.saturating_sub(HISTORY_LINES / 2);
    }
    if keys.just_pressed(KeyCode::PageDown) {
        view.scroll += HISTORY_LINES / 2;
    }
    if keys.just_pressed(KeyCode::Home) {
        view.scroll = 0;
    }
}

/// Mouse wheel scrolls the history panel (3 lines per notch; wheel up = newer).
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
    view.wheel -= notches * 3.0;
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

    let selected = menu.selected.min(menu.options.len() - 1);
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
            ui.dialogue = Some(DialogueView {
                partner: title,
                lines: vec![(String::new(), text)],
                waiting: false,
            });
        }
        Command::History { person } => {
            ui.menu = None;
            ui.history = Some(HistoryView::new(person));
        }
        Command::StartBuild(placement) => {
            ui.menu = None;
            ui.toast(format!(
                "Placing {}: walk to move the outline, [E] build here, [Esc] cancel.",
                placement.label
            ));
            ui.build = Some(placement);
        }
        Command::Talk { person } => {
            ui.menu = None;
            if !ensure_online(world, ui) {
                return;
            }
            let mut params = HashMap::new();
            params.insert("a".to_string(), world.player.name.clone());
            params.insert("b".to_string(), person.clone());
            params.insert("l".to_string(), world.current_location.name.clone());
            net.execute("talk", params);
            ui.dialogue = Some(DialogueView {
                partner: person,
                lines: Vec::new(),
                waiting: true,
            });
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
        Text::new(""),
        TextFont {
            font_size: 18.0,
            ..default()
        },
        TextColor(Color::srgb(0.94, 0.95, 0.92)),
        Node {
            position_type: PositionType::Absolute,
            top: percent(22),
            right: percent(8),
            min_width: px(300),
            max_width: px(420),
            padding: UiRect::all(px(16)),
            border: UiRect::all(px(2)),
            ..default()
        },
        BackgroundColor(Color::srgba(0.06, 0.08, 0.11, 0.94)),
        BorderColor::all(Color::srgb(0.55, 0.70, 0.85)),
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
        Visibility::Hidden,
        DialoguePanel,
    ));
    commands.spawn((
        Text::new(""),
        TextFont {
            font_size: 15.0,
            ..default()
        },
        TextColor(Color::srgb(0.92, 0.94, 0.90)),
        Node {
            position_type: PositionType::Absolute,
            top: px(90),
            left: percent(5),
            width: percent(70),
            bottom: px(20),
            padding: UiRect::all(px(14)),
            border: UiRect::all(px(2)),
            overflow: Overflow::clip(),
            ..default()
        },
        BackgroundColor(Color::srgba(0.04, 0.05, 0.08, 0.95)),
        BorderColor::all(Color::srgb(0.45, 0.75, 0.65)),
        GlobalZIndex(5),
        Visibility::Hidden,
        HistoryPanel,
    ));
}

fn short_clock(clock: &str) -> &str {
    clock.split(" (").next().unwrap_or(clock)
}

fn history_lines(view: &HistoryView) -> Vec<String> {
    let mut lines = Vec::new();
    let Some(data) = view.data.as_ref() else {
        lines.push(if view.error.is_empty() {
            "Loading...".to_string()
        } else {
            format!("Error: {}", view.error)
        });
        return lines;
    };
    let goal = if data.goal.is_empty() { "(none)" } else { data.goal.as_str() };
    lines.push(format!("NOW  [{}]  goal: {}", data.status, goal));
    if !data.goal_reason.is_empty() {
        lines.push(format!("     why: {}", trim_line(&data.goal_reason, 110)));
    }
    if !data.plan.is_empty() {
        lines.push(format!("     plan: {}", trim_line(&data.plan.join(" -> "), 110)));
    }
    lines.push(String::new());
    if data.entries.is_empty() {
        lines.push("(nothing yet - press P to start the simulation)".to_string());
    }
    for e in &data.entries {
        let clock = short_clock(&e.clock);
        match e.kind.as_str() {
            "decision" => {
                let tag = [e.source.as_str(), e.mode.as_str()]
                    .iter()
                    .filter(|s| !s.is_empty())
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(", ");
                let tag = if tag.is_empty() { String::new() } else { format!("  ({tag})") };
                lines.push(format!("{clock}  DECIDE  {}{tag}", e.text));
                if !e.reason.is_empty() {
                    lines.push(format!("                    why: {}", trim_line(&e.reason, 100)));
                }
                for t in &e.triggers {
                    lines.push(format!("                    because: {}", trim_line(t, 96)));
                }
                if !e.plan.is_empty() {
                    lines.push(format!("                    plan: {}", trim_line(&e.plan.join(" -> "), 100)));
                }
            }
            "start" => {
                let step = if e.step.is_empty() { String::new() } else { format!("  [{}]", e.step) };
                lines.push(format!("{clock}  START   {}{step}", e.text));
            }
            "done" => {
                lines.push(format!("{clock}  DONE    {}", e.text));
                for d in &e.dialogue {
                    lines.push(format!("                    \"{}\"", trim_line(d, 100)));
                }
            }
            "failed" => {
                lines.push(format!("{clock}  FAILED  {}", e.text));
                if !e.message.is_empty() {
                    lines.push(format!("                    {}", trim_line(&e.message, 100)));
                }
            }
            "replan" => {
                lines.push(format!("{clock}  REPLAN  {}", e.text));
                if !e.reason.is_empty() {
                    lines.push(format!("                    why: {}", trim_line(&e.reason, 100)));
                }
            }
            "state" => {
                let why = if e.reason.is_empty() { String::new() } else { format!("  ({})", e.reason) };
                lines.push(format!("{clock}  STATE   {}{why}", e.text));
            }
            other => lines.push(format!("{clock}  {}  {}", other.to_uppercase(), e.text)),
        }
    }
    lines
}

pub fn update_history_panel(
    mut ui: ResMut<UiState>,
    mut query: Query<(&mut Text, &mut Visibility), With<HistoryPanel>>,
) {
    let Ok((mut text, mut vis)) = query.single_mut() else {
        return;
    };
    let Some(view) = ui.history.as_mut() else {
        set_visible(&mut vis, false);
        return;
    };
    set_visible(&mut vis, true);
    let lines = history_lines(view);
    let max_scroll = lines.len().saturating_sub(HISTORY_LINES);
    view.scroll = view.scroll.min(max_scroll);
    let end = (view.scroll + HISTORY_LINES).min(lines.len());
    let more = if end < lines.len() {
        format!("   ({} more below)", lines.len() - end)
    } else {
        String::new()
    };
    let header = format!(
        "{} - history (newest first){more}\n[A/D] other NPC   [Wheel W/S PgUp/PgDn] scroll   [P] sim   [T] +1h   [H/Esc] close\n\n",
        view.name
    );
    set_text(&mut text, header + &lines[view.scroll..end].join("\n"));
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
    mut query: Query<&mut Text, With<StatusText>>,
) {
    let Ok(mut text) = query.single_mut() else {
        return;
    };
    let value = if ui.debug_visible {
        String::new()
    } else if !world.online {
        "Offline — waiting for server   [~] Debug".to_string()
    } else {
        format!(
            "{}  ·  {}  ·  HP {}      [I] Bag ({})   [B] Build   [M] Map   [P] NPCs: {}   [T] +1h   [H] History   [~] Debug",
            world.player.name,
            world.current_location.name,
            world.player.hp,
            world.held_items().len(),
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
    set_text(
        &mut text,
        format!(
            "Day {}  {:02}:{:02}\n{}{}",
            clock.day.max(1),
            total_minutes / 60,
            total_minutes % 60,
            clock.phase_label,
            state,
        ),
    );
}

pub fn update_prompt(
    nearby: Res<NearbyInteract>,
    ui: Res<UiState>,
    mut query: Query<&mut Text, With<PromptText>>,
) {
    let Ok(mut text) = query.single_mut() else {
        return;
    };
    let value = match (&ui.build, &nearby.target) {
        (Some(build), _) => format!("Placing {}   [E] build here   [Esc] cancel", build.label),
        (None, Some(target)) if !ui.blocks_movement() => format!("[E]  {}", target.prompt),
        _ if !ui.travel.is_empty() => format!("Travelling... next: {}", ui.travel[0]),
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

pub fn update_menu_panel(
    ui: Res<UiState>,
    mut query: Query<(&mut Text, &mut Visibility), With<MenuPanel>>,
) {
    let Ok((mut text, mut vis)) = query.single_mut() else {
        return;
    };
    let Some(menu) = &ui.menu else {
        set_visible(&mut vis, false);
        return;
    };
    set_visible(&mut vis, true);
    let mut body = menu.title.clone();
    if !menu.subtitle.is_empty() {
        body.push('\n');
        body.push_str(&menu.subtitle);
    }
    body.push_str("\n\n");
    for (i, option) in menu.options.iter().enumerate() {
        let cursor = if i == menu.selected { ">" } else { " " };
        body.push_str(&format!("{cursor} {}. {}\n", i + 1, option.label));
    }
    let back = if menu.parent.is_some() { "back" } else { "close" };
    body.push_str(&format!("\n[W/S] choose  [E] select  [Esc] {back}"));
    set_text(&mut text, body);
}

pub fn update_dialogue_panel(
    ui: Res<UiState>,
    mut query: Query<(&mut Text, &mut Visibility), With<DialoguePanel>>,
) {
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
    if view.waiting {
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
        body.push_str("[E / Esc] close");
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
        "AITown3 [{mode}]  sim {sim}  tick {tick}  {clock}\n{player} @ {loc}  HP {hp}\n{desc}\n\nPeople: {people}\nItems: {items}\nBag: {bag}\nExits: {exits}\n\nNPC activity:\n{activity}\n\nVillage news:\n{news}\n\nWASD walk | E interact | I bag | P sim (Shift+P: LLM) | T +1h (Shift+T: +6h) | R refresh | ~ debug\n{message}",
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
