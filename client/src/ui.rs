use bevy::input::mouse::{AccumulatedMouseScroll, MouseScrollUnit};
use bevy::prelude::*;
use bevy::ui::FocusPolicy;
use std::{
    collections::{HashMap, HashSet},
    time::{Duration, Instant},
};

use crate::adapter::thought_line;
use crate::net::{trim_line, Blueprint, MindData, RecipeView, RequestView, SaveInfo, WorldInfo, TradeGood, TradeView, DirectorData, DraftSummary, History, HistoryEntry, NetBridge, WorldSnapshot};
use crate::scene::{cell_at, cell_center, Interactive, PlayerAvatar, TargetKind, WalkGrid, ZoneGrid, TILE};

const TOAST_DURATION: Duration = Duration::from_secs(4);
const HISTORY_POLL: Duration = Duration::from_secs(1);
/// History entries shown at once (the wheel scrolls through the rest).
const HISTORY_ENTRIES: usize = 40;
/// How close (px) a click must land to a person / thing to count as clicking it.
const CLICK_RADIUS: f32 = 40.0;
const MENU_WIDTH: f32 = 320.0;
/// Manual save slots (the server also keeps an "autosave").
const SAVE_SLOTS: [&str; 3] = ["slot1", "slot2", "slot3"];
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
    /// Director tab (same panel spot as the NPC history; tabs switch between them).
    pub director: Option<DirectorView>,
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
    /// The trader whose shop menu is open (refreshed after each buy / sell).
    pub trading_with: Option<String>,
    /// The Craft panel is open (refreshed after each craft).
    pub crafting_open: bool,
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
            director: None,
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
            trading_with: None,
            crafting_open: false,
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
    /// History / Memories / Knows.
    pub page: NpcPage,
    pub mind: Option<MindData>,
    pub mind_error: String,
    pub mind_in_flight: bool,
    pub mind_fetch: Option<Instant>,
    /// Memory filter: one type ("" = all) and a search typed by the player.
    pub mind_kind: String,
    pub mind_query: String,
    /// The search box has the keyboard.
    pub searching: bool,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum NpcPage {
    Profile,
    /// What they have been doing (decisions, steps, conversations).
    #[default]
    History,
    Memories,
    Knows,
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
            page: NpcPage::History,
            mind: None,
            mind_error: String::new(),
            mind_in_flight: false,
            mind_fetch: None,
            mind_kind: String::new(),
            mind_query: String::new(),
            searching: false,
        }
    }
}

impl UiState {
    pub fn toast(&mut self, text: impl Into<String>) {
        self.toast = text.into();
        self.toast_at = Instant::now();
    }

    pub fn blocks_movement(&self) -> bool {
        self.menu.is_some()
            || self.dialogue.is_some()
            || self.history.is_some()
            || self.director.is_some()
            || self.world_map.is_some()
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
        self.trading_with = None;
        self.crafting_open = false;
    }

    /// Craft panel: what you can make now, and what each recipe still needs.
    pub fn open_craft_menu(&mut self, recipes: Vec<RecipeView>) {
        let ready = recipes.iter().filter(|r| r.ready).count();
        let mut menu = Menu::new("Craft", format!("{ready} of {} recipes ready", recipes.len()));
        for r in recipes {
            let parts: Vec<String> = r
                .inputs
                .iter()
                .map(|i| format!("{} {}/{}", i.kind, i.have, i.need))
                .chain((!r.station.is_empty()).then(|| {
                    format!("at a {}{}", r.station, if r.at_station { "" } else { " (not here)" })
                }))
                .collect();
            let label = format!("{}{}  -  {}", if r.ready { "" } else { "(missing) " }, r.label, parts.join(", "));
            let command = if r.ready {
                Command::Do { action: r.action.clone(), target: String::new(), other: String::new() }
            } else {
                Command::Examine { title: r.label.clone(), text: format!("{}\n\nNeeds: {}", r.description, parts.join(", ")) }
            };
            menu.add(label, command);
        }
        if menu.options.is_empty() {
            menu.add("(no recipes)", Command::Close);
        }
        menu.add("Close", Command::Close);
        self.open_menu(menu, None);
        self.crafting_open = true;
    }

    /// Requests people posted when they got stuck: deliver what they need and get paid.
    pub fn open_requests_menu(&mut self, list: Vec<RequestView>) {
        let open = list.iter().filter(|r| r.status == "open").count();
        let mut menu = Menu::new("Requests", format!("{open} open  ·  bring the thing to whoever asked"));
        for r in list.iter().filter(|r| r.status == "open") {
            if r.you_have.is_empty() {
                let why = if r.reason.is_empty() { String::new() } else { format!("\n\nWhy: {}", r.reason) };
                menu.add(
                    format!("{}  (you have none)", r.line),
                    Command::Examine { title: r.line.clone(), text: format!("Get some {} and bring it to {}.{why}", r.kind, r.poster) },
                );
            } else {
                menu.add(format!("Deliver: {}  (you have {})", r.line, r.you_have.join(", ")), Command::Deliver { id: r.id.clone() });
            }
        }
        for r in list.iter().filter(|r| r.status != "open").take(5) {
            let who = if r.taker.is_empty() { r.note.clone() } else { format!("by {}", r.taker) };
            menu.add(format!("({}) {}  {who}", r.status, r.line), Command::Close);
        }
        if list.is_empty() {
            menu.add("(nobody needs anything right now)", Command::Close);
        }
        menu.add("Close", Command::Close);
        self.open_menu(menu, None);
    }

    /// A trader's shop: buy their goods, sell yours for half their worth.
    pub fn open_trade_menu(&mut self, view: TradeView) {
        let npc = view.npc.clone();
        let mut menu = Menu::new(
            format!("Trade with {npc}"),
            format!("You have {} coins  ·  {npc} has {}", view.my_coins, view.npc_coins),
        );
        if !view.trader {
            menu.add(format!("{npc} doesn't trade."), Command::Close);
        }
        for (g, n) in group_goods(&view.sells) {
            let label = format!("Buy {}{}  -  {} coins{}", g.label, count(n), g.price, if g.affordable { "" } else { " (can't afford)" });
            menu.add(label, Command::Do { action: "buy".into(), target: g.name.clone(), other: npc.clone() });
        }
        if view.trader {
            for (g, n) in group_goods(&view.buys) {
                let label = format!("Sell {}{}  -  {} coins{}", g.label, count(n), g.price, if g.affordable { "" } else { " (they can't pay)" });
                menu.add(label, Command::Do { action: "sell".into(), target: g.name.clone(), other: npc.clone() });
            }
        }
        menu.add("Close", Command::Close);
        self.open_menu(menu, None);
        self.trading_with = Some(npc);
    }

    /// Pick a template world; loading it (after a confirmation) clears the current one.
    pub fn open_worlds_menu(&mut self, worlds: Vec<WorldInfo>) {
        let mut menu = Menu::new(
            "Switch world",
            "Loading a template clears the current world, NPC progress and memories - save first to keep them",
        );
        for w in worlds {
            let title = if w.title.is_empty() { w.id.clone() } else { w.title.clone() };
            let mut confirm = Menu::new(
                format!("Load {title}?"),
                "This clears the current world, NPC progress and memories.",
            );
            confirm.add("Yes, load it", Command::LoadWorld(w.id.clone()));
            confirm.add("Back", Command::Back);
            let label = if w.premise.is_empty() {
                title
            } else {
                format!("{title}  -  {}", trim_line(&w.premise, 70))
            };
            menu.add(label, Command::Submenu(Box::new(confirm)));
        }
        menu.add("Close", Command::Close);
        self.open_menu(menu, None);
    }

    /// Save into one of the slots, or load any save (autosave included) after a confirmation.
    pub fn open_save_menu(&mut self, saves: Vec<SaveInfo>) {
        let describe = |s: &SaveInfo| {
            let when = s.saved_at.replace('T', " ");
            let who = if s.player.is_empty() { String::new() } else { format!("{}, ", s.player) };
            if s.clock.is_empty() { format!("{who}{when}") } else { format!("{who}{}  ·  saved {when}", s.clock) }
        };
        let mut menu = Menu::new("Save / Load", "Saves keep the world, what every NPC is doing, and their memories");
        for slot in SAVE_SLOTS {
            let label = match saves.iter().find(|s| s.slot == slot) {
                Some(s) => format!("Save to {slot}  (overwrite: {})", describe(s)),
                None => format!("Save to {slot}  (empty)"),
            };
            menu.add(label, Command::SaveGame(slot.to_string()));
        }
        for s in &saves {
            let mut confirm = Menu::new(
                format!("Load {}?", s.slot),
                "This replaces the current world, NPC progress and memories.",
            );
            confirm.add("Yes, load it", Command::LoadGame(s.slot.clone()));
            confirm.add("Back", Command::Back);
            menu.add(format!("Load {}  ({})", s.slot, describe(s)), Command::Submenu(Box::new(confirm)));
        }
        menu.add("Close", Command::Close);
        self.open_menu(menu, None);
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
    /// Gather / craft / buy / sell through the server (it picks the materials).
    Do {
        action: String,
        target: String,
        other: String,
    },
    /// Open a trader's shop.
    Trade {
        person: String,
    },
    Party {
        person: String,
        join: bool,
    },
    Ally {
        person: String,
    },
    Deliver {
        id: String,
    },
    SaveGame(String),
    LoadGame(String),
    LoadWorld(String),
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
            let mut subtitle = match actor {
                Some(a) if !a.alive => format!("{} (down)", a.role),
                Some(a) if a.mood.is_empty() => format!("{}  HP {}", a.role, a.hp),
                Some(a) => format!("{}  HP {}  mood: {}", a.role, a.hp, a.mood),
                None => String::new(),
            };
            if let Some(a) = actor {
                if a.allies.iter().any(|x| x == me) {
                    subtitle.push_str("  ·  your ally");
                }
                if a.follows == me {
                    subtitle.push_str("  ·  in your party");
                }
                if a.trader {
                    subtitle.push_str("  ·  trader");
                }
            }
            // Hostility comes with the seamless-world snapshot (the person entry there).
            if let Some(p) = world.world.persons.iter().find(|p| &p.name == name) {
                if !p.hostile.is_empty() {
                    subtitle.push_str(&format!("  ·  HOSTILE ({})", p.hostile));
                }
            }
            let mut menu = Menu::new(name.clone(), subtitle);
            if alive {
                menu.add("Talk", Command::Talk { person: name.clone() });
                if actor.is_some_and(|a| a.trader) {
                    menu.add("Trade", Command::Trade { person: name.clone() });
                }
                if actor.is_some_and(|a| a.follows == me) {
                    menu.add("Ask them to go their own way", Command::Party { person: name.clone(), join: false });
                } else {
                    menu.add("Invite to travel with you", Command::Party { person: name.clone(), join: true });
                }
                if !actor.is_some_and(|a| a.allies.iter().any(|x| x == me)) {
                    menu.add("Propose an alliance", Command::Ally { person: name.clone() });
                }
                if !held.is_empty() {
                    let mut give = Menu::new(format!("Give to {name}"), "What do you hand over?");
                    for group in group_items(&held) {
                        give.add(
                            group.label(),
                            act("give", &[("giver", me), ("i", &group.first), ("receiver", name), ("l", here)]),
                        );
                    }
                    give.add("Back", Command::Back);
                    menu.add("Give...", Command::Submenu(Box::new(give)));
                }
            }
            let useful: Vec<&String> = held.iter().filter(|t| world.recommended_tools(name).contains(t)).collect();
            for group in group_items(useful) {
                menu.add(format!("Use {} on {name}  *", group.base), use_on(me, &group.first, name, here));
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
            let theirs = !thing.owner.is_empty() && thing.owner != me;
            if theirs {
                subtitle = format!("{subtitle}  ·  {}'s", thing.owner).trim().to_string();
            } else if thing.owner == me {
                subtitle = format!("{subtitle}  ·  yours").trim().to_string();
            }
            let mut menu = Menu::new(name, subtitle);
            if thing.can_pick_up() {
                // Someone else's: taking it is stealing (anyone who sees it will remember).
                let label = if theirs { format!("Steal ({}'s)", thing.owner) } else { "Pick up".to_string() };
                menu.add(label, act("pick_up", &[("c", me), ("i", name), ("l", here)]));
            }
            // The object's own actions: what you can do now, and why the rest can't be done.
            for action in &thing.actions {
                if action.available {
                    // "do:<action>" = a gathering action (the server picks the tool); else an authored one
                    let command = match action.id.strip_prefix("do:") {
                        Some(content_action) => Command::Do {
                            action: content_action.to_string(),
                            target: name.to_string(),
                            other: String::new(),
                        },
                        None => act("interact", &[("c", me), ("target", name), ("option", &action.id)]),
                    };
                    menu.add(action.label.clone(), command);
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
            // Tools that are known to work on it go straight in the card; anything else you
            // carry is one "Use something else..." entry, not a line per item.
            let recommended = world.recommended_tools(name);
            let (useful, other): (Vec<&String>, Vec<&String>) = held.iter().partition(|t| recommended.contains(t));
            for group in group_items(useful) {
                menu.add(format!("Use {} on {name}  *", group.base), use_on(me, &group.first, name, here));
            }
            if !other.is_empty() {
                let mut sub = Menu::new(format!("Use on {name}"), "Try something you carry");
                for group in group_items(other) {
                    sub.add(group.label(), use_on(me, &group.first, name, here));
                }
                sub.add("Back", Command::Back);
                menu.add("Use something else on it...", Command::Submenu(Box::new(sub)));
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

/// Several of the same thing ("Berry1", "Berry2", "Berry3"): one menu line, the first one stands in.
struct ItemGroup {
    base: String,
    count: usize,
    first: String,
}

impl ItemGroup {
    fn label(&self) -> String {
        format!("{}{}", self.base, count(self.count))
    }
}

fn count(n: usize) -> String {
    if n > 1 { format!(" x{n}") } else { String::new() }
}

/// The name without its running number: "Berry3" / "Berry_3" / "Berry 3" -> "Berry".
fn item_base(name: &str) -> &str {
    let trimmed = name.trim_end_matches(|c: char| c.is_ascii_digit());
    let trimmed = trimmed.trim_end_matches(['_', ' ', '-', '#']);
    if trimmed.is_empty() { name } else { trimmed }
}

fn group_items<'a>(names: impl IntoIterator<Item = &'a String>) -> Vec<ItemGroup> {
    let mut out: Vec<ItemGroup> = Vec::new();
    for name in names {
        let base = item_base(name);
        match out.iter_mut().find(|g| g.base == base) {
            Some(g) => g.count += 1,
            None => out.push(ItemGroup { base: base.to_string(), count: 1, first: name.clone() }),
        }
    }
    out
}

/// Goods with the same label and price are one line in the shop.
fn group_goods(goods: &[TradeGood]) -> Vec<(&TradeGood, usize)> {
    let mut out: Vec<(&TradeGood, usize)> = Vec::new();
    for g in goods {
        match out.iter_mut().find(|(o, _)| o.label == g.label && o.price == g.price) {
            Some((_, n)) => *n += 1,
            None => out.push((g, 1)),
        }
    }
    out
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
    for group in group_items(&held) {
        let mut sub = Menu::new(group.label(), "In your bag");
        sub.add("Use one", act("use_item", &[("c", me), ("i", &group.first)]));
        sub.add("Drop one", act("drop", &[("c", me), ("i", &group.first), ("l", here)]));
        sub.add("Back", Command::Back);
        menu.add(group.label(), Command::Submenu(Box::new(sub)));
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

    if ui.director.is_some() {
        if ui.director.as_ref().is_some_and(|d| d.typing_theme) {
            // Typing the questline theme: keys belong to the box (see text_box_typing).
            if left || right {
                if let Some(view) = ui.director.as_mut() {
                    view.typing_theme = false;
                }
            }
            return;
        }
        if left || right || keys.just_pressed(KeyCode::Escape) {
            ui.director = None;
            return;
        }
        global_keys(&keys, &mut ui, &mut net);
        return;
    }

    if ui.history.is_some() {
        if ui.history.as_ref().is_some_and(|v| v.searching) {
            // Typing a memory search: keys belong to the search box (see mind_search_typing).
            if left || right {
                if let Some(view) = ui.history.as_mut() {
                    view.searching = false;
                }
            }
            return;
        }
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
        let page = view.page;
        *view = HistoryView::new(names[next].clone());
        view.page = page;
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
        Command::Do { action, target, other } => {
            // Shops and the craft panel stay open and refresh after the deal.
            if ensure_online(world, ui) {
                net.do_action(&action, &target, &other);
            }
        }
        Command::Trade { person } => {
            if ensure_online(world, ui) {
                net.fetch_trade(&person);
            }
        }
        Command::Party { person, join } => {
            ui.menu = None;
            if ensure_online(world, ui) {
                net.party(&person, join);
            }
        }
        Command::Deliver { id } => {
            ui.menu = None;
            if ensure_online(world, ui) {
                net.deliver(&id);
            }
        }
        Command::Ally { person } => {
            ui.menu = None;
            if ensure_online(world, ui) {
                net.propose_alliance(&person);
            }
        }
        Command::SaveGame(slot) => {
            ui.menu = None;
            if ensure_online(world, ui) {
                net.save_game(&slot);
                ui.toast(format!("Saving to {slot}..."));
            }
        }
        Command::LoadGame(slot) => {
            ui.menu = None;
            ui.cancel_travel();
            if ensure_online(world, ui) {
                net.load_game(&slot);
                ui.toast(format!("Loading {slot}..."));
            }
        }
        Command::LoadWorld(id) => {
            ui.menu = None;
            ui.cancel_travel();
            if ensure_online(world, ui) {
                net.load_world(&id);
                ui.toast(format!("Building world {id}..."));
            }
        }
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

#[derive(Component)]
pub struct DirectorPanel;

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
            top: px(70),
            left: percent(5),
            width: percent(70),
            bottom: px(64),
            flex_direction: FlexDirection::Column,
            padding: UiRect::all(px(12)),
            border: UiRect::all(px(2)),
            overflow: Overflow::clip(),
            ..default()
        },
        BackgroundColor(Color::srgba(0.05, 0.04, 0.08, 0.96)),
        BorderColor::all(Color::srgb(0.75, 0.55, 0.85)),
        Interaction::default(),
        FocusPolicy::Block,
        GlobalZIndex(5),
        Visibility::Hidden,
        DirectorPanel,
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
            "{}  ·  {}  ·  HP {}  ·  {} coins{}   ·   NPCs {}",
            world.player.name,
            world.current_location.name,
            world.player.hp,
            world.player.coins,
            party_text(&world),
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
    // Story and quests live in the Director tab; the HUD only shows the time.
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
    Craft,
    Requests,
    SaveMenu,
    Worlds,
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
    /// Panel tabs: NPC history / director.
    TabNpcs,
    TabDirector,
    /// NPC tab pages and the memory filter / search box.
    NpcPage(NpcPage),
    /// Switch the open character between "llm" and "routine".
    SetBrain(String),
    MemoryKind(String),
    MemorySearch,
    ClearMemorySearch,
    NudgeDirector,
    ThemeBox,
    DraftQuestline,
    ApplyQuestline,
    DiscardDraft,
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
        UiAction::Requests => {
            if ensure_online(world, ui) {
                net.fetch_requests();
            }
        }
        UiAction::Craft => {
            if ui.crafting_open && ui.menu.is_some() {
                ui.menu = None;
                ui.crafting_open = false;
            } else if ensure_online(world, ui) {
                net.fetch_crafting();
            }
        }
        UiAction::SaveMenu => {
            if ensure_online(world, ui) {
                net.fetch_saves();
            }
        }
        UiAction::Worlds => {
            if ensure_online(world, ui) {
                net.fetch_worlds();
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
                let page = view.page;
                *view = HistoryView::new(names[next].clone());
                view.page = page;
            }
        }
        UiAction::CloseHistory => {
            ui.history = None;
            ui.director = None;
        }
        UiAction::NpcPage(page) => {
            if let Some(view) = ui.history.as_mut() {
                view.page = page;
                view.searching = false;
                view.mind_fetch = None;
                view.scroll = 0;
            }
        }
        UiAction::SetBrain(brain) => {
            if let Some(view) = ui.history.as_mut() {
                net.set_brain(&view.name, &brain);
                if let Some(mind) = view.mind.as_mut() {
                    mind.profile.brain = brain; // show it right away; the next fetch confirms
                }
                view.mind_fetch = None;
            }
        }
        UiAction::MemoryKind(kind) => {
            if let Some(view) = ui.history.as_mut() {
                view.mind_kind = kind;
                view.mind_fetch = None;
                view.scroll = 0;
            }
        }
        UiAction::MemorySearch => {
            if let Some(view) = ui.history.as_mut() {
                view.searching = !view.searching;
                if !view.searching {
                    view.mind_fetch = None;
                }
            }
        }
        UiAction::ClearMemorySearch => {
            if let Some(view) = ui.history.as_mut() {
                view.mind_query.clear();
                view.searching = false;
                view.mind_fetch = None;
            }
        }
        UiAction::TabNpcs => {
            ui.director = None;
            if ui.history.is_none() {
                if let Some(name) = npc_names(world).into_iter().next() {
                    ui.history = Some(HistoryView::new(name));
                }
            }
        }
        UiAction::TabDirector => {
            ui.history = None;
            ui.menu = None;
            ui.world_map = None;
            if ui.director.is_none() {
                ui.director = Some(DirectorView::default());
            }
        }
        UiAction::NudgeDirector => {
            net.nudge_director();
            ui.toast("The director will make a move on the next tick (if NPCs are running).");
            if let Some(view) = ui.director.as_mut() {
                view.last_fetch = None;
            }
        }
        UiAction::ThemeBox => {
            if let Some(view) = ui.director.as_mut() {
                view.typing_theme = !view.typing_theme;
            }
        }
        UiAction::DraftQuestline => {
            if let Some(view) = ui.director.as_mut() {
                if !view.drafting {
                    view.drafting = true;
                    view.typing_theme = false;
                    view.draft = None;
                    net.draft_questline(view.theme.trim());
                }
            }
        }
        UiAction::ApplyQuestline => {
            if let Some(view) = ui.director.as_mut() {
                if view.draft.as_ref().is_some_and(|d| d.ok) && !view.applying {
                    view.applying = true;
                    net.apply_questline();
                }
            }
        }
        UiAction::DiscardDraft => {
            if let Some(view) = ui.director.as_mut() {
                view.draft = None;
            }
        }
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
        button(p, "Craft", UiAction::Craft, 15.0, false, false);
        button(p, "Requests", UiAction::Requests, 15.0, false, false);
        button(p, "Build", UiAction::Build, 15.0, false, false);
        button(p, "Map", UiAction::Map, 15.0, false, false);
        button(p, "People", UiAction::History, 15.0, false, false);
        button(p, "Director", UiAction::TabDirector, 15.0, false, false);
        if ui.sim_running {
            button(p, "Pause NPCs", UiAction::Sim { llm: false }, 15.0, true, false);
        } else {
            button(p, "Run NPCs (rules)", UiAction::Sim { llm: false }, 15.0, false, false);
            button(p, "Run NPCs (LLM)", UiAction::Sim { llm: true }, 15.0, false, false);
        }
        button(p, "+1 hour", UiAction::AdvanceTime, 15.0, false, false);
        button(p, "Save / Load", UiAction::SaveMenu, 15.0, false, false);
        button(p, "Worlds", UiAction::Worlds, 15.0, false, false);
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
    if view.page != NpcPage::History {
        let rows = view.mind.as_ref().map_or(0, |d| d.memories.len().max(d.knows.seen.len()));
        view.scroll = view.scroll.min(rows.saturating_sub(1));
        let signature = mind_signature(view);
        if *shown == signature {
            return;
        }
        *shown = signature;
        let view = &*view;
        commands.entity(entity).despawn_related::<Children>();
        commands.entity(entity).with_children(|p| {
            tab_row(p, false);
            npc_header(p, &view.name, view.page);
            match view.page {
                NpcPage::Profile => profile_page(p, view),
                NpcPage::Memories => memories_page(p, view),
                _ => knows_page(p, view),
            }
        });
        return;
    }
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
        tab_row(p, false);
        npc_header(p, &name, view.page);
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
    let searching = ui.history.as_ref().is_some_and(|v| v.searching)
        || ui.director.as_ref().is_some_and(|v| v.typing_theme);
    if let Ok(mut window) = windows.single_mut() {
        if window.ime_enabled != (chatting || searching) {
            window.ime_enabled = chatting || searching;
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

/// Director tab state: what the server last said, plus a questline draft being reviewed.
#[derive(Default)]
pub struct DirectorView {
    pub data: Option<DirectorData>,
    pub error: String,
    pub in_flight: bool,
    pub last_fetch: Option<Instant>,
    pub draft: Option<DraftSummary>,
    pub drafting: bool,
    pub applying: bool,
    pub scroll: usize,
    /// What the player wants the questline to be about, and whether its box has the keyboard.
    pub theme: String,
    pub typing_theme: bool,
}

const DIRECTOR_POLL: Duration = Duration::from_secs(2);

/// "NPCs | Director" tabs at the top of the big panel.
fn tab_row(p: &mut ChildSpawnerCommands, director: bool) {
    p.spawn(Node { flex_direction: FlexDirection::Row, column_gap: px(4), margin: UiRect::bottom(px(6)), ..default() })
        .with_children(|row| {
            button(row, "People", UiAction::TabNpcs, 15.0, !director, false);
            button(row, "Director", UiAction::TabDirector, 15.0, director, false);
            row.spawn(Node { flex_grow: 1.0, ..default() });
            button(row, "Close", UiAction::CloseHistory, 15.0, false, false);
        });
}

/// Re-fetch the director tab every couple of seconds while it is open.
pub fn poll_director(net: Res<NetBridge>, mut ui: ResMut<UiState>) {
    let Some(view) = ui.director.as_mut() else { return };
    if view.in_flight || view.last_fetch.is_some_and(|t| t.elapsed() < DIRECTOR_POLL) {
        return;
    }
    view.in_flight = true;
    view.last_fetch = Some(Instant::now());
    net.fetch_director();
}

/// Mouse wheel scrolls the director tab (one line-group per notch).
pub fn director_wheel(scroll: Res<AccumulatedMouseScroll>, mut ui: ResMut<UiState>) {
    let Some(view) = ui.director.as_mut() else { return };
    if scroll.delta.y > 0.0 {
        view.scroll = view.scroll.saturating_sub(1);
    } else if scroll.delta.y < 0.0 {
        view.scroll += 1;
    }
}

fn section(p: &mut ChildSpawnerCommands, title: &str) {
    p.spawn((
        Text::new(title.to_string()),
        TextFont { font_size: 16.0, ..default() },
        TextColor(TITLE),
        Node { margin: UiRect::top(px(10)), ..default() },
    ));
}

fn line(p: &mut ChildSpawnerCommands, text: String, color: Color, size: f32) {
    p.spawn((Text::new(text), TextFont { font_size: size, ..default() }, TextColor(color)));
}

fn director_signature(view: &DirectorView) -> String {
    let Some(d) = view.data.as_ref() else {
        return format!("loading|{}|{}", view.error, view.scroll);
    };
    let lines = |v: &Vec<crate::net::DirectorLine>| v.iter().map(|l| format!("{}{}", l.clock, l.line)).collect::<Vec<_>>().join(";");
    let quests: Vec<String> = d
        .questlines
        .iter()
        .map(|q| format!("{}{}{}", q.title, q.state, q.stages.iter().map(|s| s.state.as_str()).collect::<String>()))
        .collect();
    format!(
        "{}|{}|{}|{}|{}|{:?}|{}|{}|{}|{}|{}|{}|{}|{}|{:?}|{}|{}",
        d.market.iter().map(|m| format!("{}{}{}", m.kind, m.price, m.supply)).collect::<String>()
            + &d.requests.iter().map(|r| r.line.clone()).collect::<String>()
            + &d.alliances.iter().map(|a| format!("{}{}{:?}", a.name, a.members.join(","), a.stances.iter().map(|s| s.value).collect::<Vec<_>>())).collect::<String>(),
        d.mode,
        d.thinking,
        d.arc,
        d.next_clock,
        d.next_in_hours.map(|h| (h * 4.0) as i32),
        lines(&d.beats),
        lines(&d.conflicts),
        lines(&d.tensions),
        lines(&d.chronicle),
        quests.join(";"),
        view.error,
        view.drafting,
        view.applying,
        view.draft.as_ref().map(|dr| format!(
            "{}{}{}",
            dr.lines.join(";"),
            dr.sections.iter().map(|s| s.lines.join(";")).collect::<String>(),
            dr.problems.join(";")
        )),
        format!("{}{}", view.theme, view.typing_theme),
        view.scroll,
    )
}

pub fn update_director_panel(
    mut commands: Commands,
    ui: Res<UiState>,
    mut panel: Query<(Entity, &mut Visibility), With<DirectorPanel>>,
    mut shown: Local<String>,
) {
    let Ok((entity, mut vis)) = panel.single_mut() else { return };
    let Some(view) = ui.director.as_ref() else {
        set_visible(&mut vis, false);
        if !shown.is_empty() {
            shown.clear();
            commands.entity(entity).despawn_related::<Children>();
        }
        return;
    };
    set_visible(&mut vis, true);
    let signature = director_signature(view);
    if *shown == signature {
        return;
    }
    *shown = signature;
    commands.entity(entity).despawn_related::<Children>();
    commands.entity(entity).with_children(|p| {
        tab_row(p, true);
        // Everything below scrolls with the wheel.
        p.spawn(Node {
            flex_direction: FlexDirection::Column,
            top: px(-(view.scroll as f32) * 48.0),
            ..default()
        })
        .with_children(|body| director_body(body, view));
    });
}

fn director_body(p: &mut ChildSpawnerCommands, view: &DirectorView) {
    if !view.error.is_empty() {
        line(p, view.error.clone(), Color::srgb(0.95, 0.5, 0.45), 14.0);
    }
    let Some(d) = view.data.as_ref() else {
        line(p, "Loading...".into(), MUTED, 15.0);
        return;
    };

    // The storyteller: where the story is going, when it acts next.
    let mode = match d.mode.as_str() {
        "llm" => "LLM storyteller",
        "template" => "template storyteller (no LLM: run NPCs in LLM mode for a real one)",
        _ => "not running - start the NPCs to wake the storyteller",
    };
    line(p, format!("Storyteller: {mode}"), MUTED, 14.0);
    let arc = if d.arc.is_empty() { "(no story arc yet)" } else { d.arc.as_str() };
    line(p, format!("Story so far: {arc}"), TEXT, 17.0);
    if d.thinking {
        line(p, "The storyteller is thinking about the next move...".into(), SAID, 14.0);
    } else if let Some(hours) = d.next_in_hours {
        line(p, format!("Next move around {} (in {:.1} game hours)", d.next_clock, hours), MUTED, 14.0);
    }
    p.spawn(Node { flex_direction: FlexDirection::Row, column_gap: px(6), margin: UiRect::top(px(4)), ..default() })
        .with_children(|row| {
            if d.mode != "off" {
                button(row, "Make a move now", UiAction::NudgeDirector, 14.0, false, false);
            }
        });

    // The quest designer: a whole questline with its own places, people and items.
    section(p, "Quest designer");
    line(
        p,
        "Writes a complete questline - new places, people (with their own day), items, clues and stages - \
         then checks with the planner that it can really be finished.".into(),
        MUTED,
        13.0,
    );
    p.spawn(Node { flex_direction: FlexDirection::Row, column_gap: px(6), margin: UiRect::top(px(4)), ..default() })
        .with_children(|row| {
            let caret = if view.typing_theme { "|" } else { "" };
            let theme = if view.theme.is_empty() && !view.typing_theme {
                "Theme (optional): click and type, e.g. a missing child".to_string()
            } else {
                format!("Theme: {}{caret}", view.theme)
            };
            button(row, theme, UiAction::ThemeBox, 14.0, view.typing_theme, false);
            let label = if view.drafting { "Designing... (a minute or two)" } else { "Design a questline" };
            button(row, label, UiAction::DraftQuestline, 14.0, view.drafting, false);
        });

    // A questline draft to review before it goes into the world.
    if let Some(draft) = view.draft.as_ref() {
        let tries = draft.attempts.map(|n| format!(", {n} tries")).unwrap_or_default();
        section(p, &if draft.ok {
            format!("Draft: checked, it can be completed{tries}")
        } else {
            format!("Draft: could not make it work{tries}")
        });
        if !draft.title.is_empty() {
            line(p, format!("  {}", draft.title), SAID, 17.0);
        }
        if !draft.premise.is_empty() {
            line(p, format!("  {}", draft.premise), TEXT, 14.0);
        }
        if !draft.hook.is_empty() {
            line(p, format!("  How it starts: {}", draft.hook), MUTED, 13.0);
        }
        for sec in &draft.sections {
            line(p, format!("  {}", sec.title), TITLE, 14.0);
            for text in &sec.lines {
                let color = if text.starts_with("    ") { MUTED } else { TEXT };
                line(p, format!("    {text}"), color, 13.0);
            }
        }
        for problem in &draft.problems {
            line(p, format!("  Problem: {problem}"), Color::srgb(0.95, 0.6, 0.5), 13.0);
        }
        p.spawn(Node { flex_direction: FlexDirection::Row, column_gap: px(6), margin: UiRect::top(px(4)), ..default() })
            .with_children(|row| {
                if draft.ok {
                    let label = if view.applying { "Adding..." } else { "Add to the world" };
                    button(row, label, UiAction::ApplyQuestline, 14.0, true, false);
                }
                button(row, "Discard", UiAction::DiscardDraft, 14.0, false, false);
            });
    }

    section(p, "Questlines");
    if d.questlines.is_empty() {
        line(p, "  (none yet - write one above)".into(), MUTED, 14.0);
    }
    for q in &d.questlines {
        line(p, format!("  {}  [{}]", q.title, q.state), SAID, 15.0);
        if !q.premise.is_empty() {
            line(p, format!("    {}", trim_line(&q.premise, 220)), MUTED, 13.0);
        }
        for s in &q.stages {
            let mark = match s.state.as_str() {
                "done" => "[x]",
                "current" => "[>]",
                _ => "[ ]",
            };
            let giver = if s.giver.is_empty() { String::new() } else { format!("  (from {})", s.giver) };
            let color = if s.state == "done" { MUTED } else { TEXT };
            line(p, format!("    {mark} {}{giver}: {}", s.title, s.objective), color, 14.0);
        }
    }

    section(p, "Alliances");
    if d.alliances.is_empty() {
        line(p, "  (none yet - people who grow close band together)".into(), MUTED, 14.0);
    }
    for a in &d.alliances {
        line(p, format!("  {}: {}", a.name, a.members.join(", ")), SAID, 15.0);
        for s in &a.stances {
            let color = match s.word.as_str() {
                "hostile" => Color::srgb(0.95, 0.5, 0.45),
                "rivals" => Color::srgb(0.95, 0.75, 0.45),
                "friendly" => Color::srgb(0.6, 0.9, 0.6),
                _ => MUTED,
            };
            line(p, format!("    with {}: {} ({})", s.other, s.word, s.value), color, 13.0);
        }
    }

    section(p, "Requests");
    if d.requests.is_empty() {
        line(p, "  (nobody has asked for help)".into(), MUTED, 14.0);
    }
    for r in &d.requests {
        line(p, format!("  {}", r.line), TEXT, 13.0);
    }

    section(p, "Economy");
    if d.market.is_empty() {
        line(p, "  (no prices yet - they are set every game hour while NPCs run)".into(), MUTED, 14.0);
    }
    for m in &d.market {
        let trend = match m.trend.as_str() {
            "up" => "  rising",
            "down" => "  falling",
            _ => "",
        };
        let color = if m.factor > 1.3 { SAID } else if m.factor < 0.8 { MUTED } else { TEXT };
        line(
            p,
            format!("  {:<14} {:>3} coins  (x{:.1})  {} around  demand {:.1}{trend}", m.kind, m.price, m.factor, m.supply, m.demand),
            color,
            13.0,
        );
    }

    section(p, "Recent moves");
    if d.beats.is_empty() {
        line(p, "  (nothing yet)".into(), MUTED, 14.0);
    }
    for b in &d.beats {
        line(p, format!("  {}   {}", b.clock, trim_line(&b.line, 200)), TEXT, 14.0);
    }

    section(p, "Conflicts");
    if d.conflicts.is_empty() && d.tensions.is_empty() {
        line(p, "  (everyone gets along, for now)".into(), MUTED, 14.0);
    }
    for c in &d.conflicts {
        line(p, format!("  {}   {}", c.clock, c.line), TEXT, 14.0);
    }
    for t in &d.tensions {
        line(p, format!("  grudge: {}", t.line), MUTED, 14.0);
    }

    if !d.chronicle.is_empty() {
        section(p, "Village news");
        for c in &d.chronicle {
            line(p, format!("  {}   {}", c.clock, trim_line(&c.line, 200)), TEXT, 14.0);
        }
    }
}

const MIND_POLL: Duration = Duration::from_secs(3);
/// Memory types in the order the filter shows them (others follow alphabetically).
const MEMORY_KINDS: [&str; 5] = ["dialogue", "fact", "action", "conflict", "event"];

/// "< Sage >" and the History / Memories / Knows page buttons.
fn npc_header(p: &mut ChildSpawnerCommands, name: &str, page: NpcPage) {
    p.spawn(Node { flex_direction: FlexDirection::Row, align_items: AlignItems::Center, column_gap: px(8), ..default() })
        .with_children(|row| {
            button(row, "<", UiAction::HistoryStep(-1), 16.0, false, false);
            row.spawn((Text::new(name.to_string()), TextFont { font_size: 20.0, ..default() }, TextColor(TITLE)));
            button(row, ">", UiAction::HistoryStep(1), 16.0, false, false);
            row.spawn(Node { width: px(16), ..default() });
            for (label, which) in [
                ("Profile", NpcPage::Profile),
                ("Activity", NpcPage::History),
                ("Memories", NpcPage::Memories),
                ("Knows", NpcPage::Knows),
            ] {
                button(row, label, UiAction::NpcPage(which), 15.0, page == which, false);
            }
        });
}

/// Fetch memories / knowledge for the open NPC page every few seconds (and right after a filter change).
pub fn poll_mind(net: Res<NetBridge>, mut ui: ResMut<UiState>) {
    let Some(view) = ui.history.as_mut() else { return };
    if view.page == NpcPage::History || view.searching || view.mind_in_flight {
        return;
    }
    if view.mind_fetch.is_some_and(|t| t.elapsed() < MIND_POLL) {
        return;
    }
    view.mind_in_flight = true;
    view.mind_fetch = Some(Instant::now());
    net.fetch_mind(&view.name, &view.mind_query, &view.mind_kind);
}

/// Typing into a text box: the memory search or the questline theme
/// (letters, IME, Backspace; Enter / Esc finishes).
pub fn mind_search_typing(
    mut keys: MessageReader<bevy::input::keyboard::KeyboardInput>,
    mut ime: MessageReader<bevy::window::Ime>,
    mut ui: ResMut<UiState>,
) {
    // Which box has the keyboard: the memory search or the questline theme.
    if let Some(view) = ui.history.as_mut().filter(|v| v.searching) {
        if type_into(&mut keys, &mut ime, &mut view.mind_query, 80) {
            view.searching = false;
            view.mind_fetch = None;
        }
    } else if let Some(view) = ui.director.as_mut().filter(|v| v.typing_theme) {
        if type_into(&mut keys, &mut ime, &mut view.theme, 160) {
            view.typing_theme = false;
        }
    } else {
        keys.clear();
        ime.clear();
    }
}

/// Feed typed text (IME included) into `text`; true when Enter / Esc ends the typing.
fn type_into(
    keys: &mut MessageReader<bevy::input::keyboard::KeyboardInput>,
    ime: &mut MessageReader<bevy::window::Ime>,
    text: &mut String,
    max: usize,
) -> bool {
    use bevy::input::keyboard::Key;
    use bevy::window::Ime;

    for event in ime.read() {
        if let Ime::Commit { value, .. } = event {
            text.push_str(value);
        }
    }
    let mut finished = false;
    for event in keys.read() {
        if !event.state.is_pressed() {
            continue;
        }
        match &event.logical_key {
            Key::Enter | Key::Escape => finished = true,
            Key::Backspace => {
                text.pop();
            }
            _ => {
                if let Some(typed) = &event.text {
                    if typed.chars().all(|c| !c.is_control()) && text.chars().count() < max {
                        text.push_str(typed);
                    }
                }
            }
        }
    }
    finished
}

fn mind_signature(view: &HistoryView) -> String {
    let data = view.mind.as_ref().map(|d| {
        let mems: Vec<String> = d.memories.iter().map(|m| format!("{}{}", m.time, m.text)).collect();
        let k = &d.knows;
        let profile: Vec<String> = d
            .profile
            .fields
            .iter()
            .map(|f| f.value.clone())
            .chain(d.profile.status.clone())
            .chain([d.profile.brain.clone()])
            .collect();
        let routine: Vec<String> = d.routine.slots.iter().map(|s| format!("{}{}{}{}", s.hours, s.what, s.now, s.done)).collect();
        format!(
            "{}|{}|{}|{}|{:?}|{}|{}|{}|{}|{}|{:?}",
            profile.join(";"),
            routine.join(";"),
            d.routine.state,
            d.total,
            d.types,
            mems.join(";"),
            format!("{}{}", k.relations.join(";"), k.facts.iter().map(|f| f.text.as_str()).collect::<Vec<_>>().join(";")),
            k.places.join(","),
            k.seen.join(";"),
            k.grudges.join(";"),
            k.trust,
        )
    });
    format!(
        "{}|{:?}|{}|{}|{}|{}|{}|{:?}",
        view.name, view.page, view.mind_kind, view.mind_query, view.searching, view.mind_error,
        view.scroll, data
    )
}

fn memories_page(p: &mut ChildSpawnerCommands, view: &HistoryView) {
    let Some(data) = view.mind.as_ref() else {
        let text = if view.mind_error.is_empty() { "Loading...".to_string() } else { format!("Error: {}", view.mind_error) };
        line(p, text, MUTED, 15.0);
        return;
    };
    if !data.available {
        line(p, "Memory store is not available on the server.".into(), MUTED, 15.0);
        return;
    }
    // Filter by type.
    p.spawn(Node { flex_direction: FlexDirection::Row, flex_wrap: FlexWrap::Wrap, column_gap: px(4), margin: UiRect::top(px(6)), ..default() })
        .with_children(|row| {
            button(row, format!("All ({})", data.total), UiAction::MemoryKind(String::new()), 13.0, view.mind_kind.is_empty(), false);
            let mut kinds: Vec<&String> = data.types.keys().collect();
            kinds.sort_by_key(|k| (MEMORY_KINDS.iter().position(|m| m == k).unwrap_or(99), k.to_string()));
            for kind in kinds {
                let n = data.types.get(kind).copied().unwrap_or(0);
                button(row, format!("{kind} ({n})"), UiAction::MemoryKind(kind.clone()), 13.0, view.mind_kind == *kind, false);
            }
        });
    // Search by meaning: the same lookup the NPC does when deciding.
    p.spawn(Node { flex_direction: FlexDirection::Row, column_gap: px(6), margin: UiRect::vertical(px(4)), ..default() })
        .with_children(|row| {
            let caret = if view.searching { "|" } else { "" };
            let label = if view.mind_query.is_empty() && !view.searching {
                "Search memories by meaning (click, type, Enter)".to_string()
            } else {
                format!("Search: {}{caret}", view.mind_query)
            };
            button(row, label, UiAction::MemorySearch, 14.0, view.searching, false);
            if !view.mind_query.is_empty() {
                button(row, "Clear", UiAction::ClearMemorySearch, 14.0, false, false);
            }
        });
    let how = if view.mind_query.is_empty() {
        "Newest first.".to_string()
    } else {
        format!("Closest in meaning to \"{}\" - what they would recall when this comes up.", view.mind_query)
    };
    line(p, how, MUTED, 13.0);
    if data.memories.is_empty() {
        line(p, "  (nothing remembered yet)".into(), MUTED, 14.0);
    }
    for m in data.memories.iter().skip(view.scroll) {
        p.spawn(Node { flex_direction: FlexDirection::Column, padding: UiRect::axes(px(8), px(3)), ..default() })
            .with_children(|b| {
                line(b, m.text.clone(), TEXT, 15.0);
                let place = if m.location.is_empty() { String::new() } else { format!("  ·  {}", m.location) };
                line(b, format!("      {}{place}  ·  {}", m.kind, m.time), MUTED, 12.0);
            });
    }
}

fn knows_page(p: &mut ChildSpawnerCommands, view: &HistoryView) {
    let Some(data) = view.mind.as_ref() else {
        let text = if view.mind_error.is_empty() { "Loading...".to_string() } else { format!("Error: {}", view.mind_error) };
        line(p, text, MUTED, 15.0);
        return;
    };
    let k = &data.knows;
    if let Some(trust) = k.trust {
        section(p, "Feelings about the player");
        let feel = match trust {
            t if t >= 5 => "trusts them a lot",
            t if t >= 2 => "likes them",
            t if t <= -2 => "distrusts them",
            _ => "doesn't know them well yet",
        };
        line(p, format!("  {} {feel} (trust {trust})", if k.player.is_empty() { "The player:" } else { k.player.as_str() }), TEXT, 15.0);
    }
    section(p, "People");
    if k.relations.is_empty() {
        line(p, "  (no strong feelings about anyone yet)".into(), MUTED, 14.0);
    }
    for r in &k.relations {
        line(p, format!("  {r}"), TEXT, 14.0);
    }
    section(p, "Facts and rumours");
    if k.facts.is_empty() {
        line(p, "  (none yet - they learn facts when someone tells them)".into(), MUTED, 14.0);
    }
    for f in &k.facts {
        let (tag, color) = if f.kind == "rumor" { ("rumour", MUTED) } else { ("fact", TEXT) };
        line(p, format!("  [{tag}] {}", f.text), color, 14.0);
    }
    section(p, "Grudges");
    if k.grudges.is_empty() {
        line(p, "  (none)".into(), MUTED, 14.0);
    }
    for g in &k.grudges {
        line(p, format!("  {g}"), TEXT, 14.0);
    }
    section(p, "Places they know");
    if k.omniscient {
        line(p, "  Knows the whole world.".into(), MUTED, 14.0);
    } else if k.places.is_empty() {
        line(p, "  (none)".into(), MUTED, 14.0);
    } else {
        line(p, format!("  {}", k.places.join(", ")), TEXT, 14.0);
    }
    if !k.seen.is_empty() {
        section(p, "Where they think things are (may be out of date)");
        for s in k.seen.iter().skip(view.scroll) {
            line(p, format!("  {s}"), TEXT, 14.0);
        }
    }
}

/// Profile page: who they are (as written), how they are now, and their day.
fn profile_page(p: &mut ChildSpawnerCommands, view: &HistoryView) {
    let Some(data) = view.mind.as_ref() else {
        let text = if view.mind_error.is_empty() { "Loading...".to_string() } else { format!("Error: {}", view.mind_error) };
        line(p, text, MUTED, 15.0);
        return;
    };
    let mut now = data.profile.status.join("  ·  ");
    if !data.routine.state.is_empty() {
        now = format!("{}  ·  {now}", data.routine.state);
    }
    if !now.is_empty() {
        line(p, now, SAID, 15.0);
    }
    if !data.profile.brain.is_empty() {
        let (what, other, label) = if data.profile.brain == "routine" {
            ("Background NPC: follows the daily routine only, never asks the LLM what to do.", "llm", "Let them think (LLM)")
        } else {
            ("LLM character: decides with the language model when something happens.", "routine", "Routine only")
        };
        p.spawn(Node { flex_direction: FlexDirection::Row, align_items: AlignItems::Center, column_gap: px(8), margin: UiRect::top(px(4)), ..default() })
            .with_children(|row| {
                row.spawn((Text::new(what.to_string()), TextFont { font_size: 14.0, ..default() }, TextColor(TEXT)));
                button(row, label, UiAction::SetBrain(other.to_string()), 13.0, false, false);
            });
    }
    section(p, "Character");
    if data.profile.fields.is_empty() {
        line(p, "  (nothing written about them)".into(), MUTED, 14.0);
    }
    for f in &data.profile.fields {
        p.spawn(Node { flex_direction: FlexDirection::Column, padding: UiRect::axes(px(8), px(2)), ..default() })
            .with_children(|b| {
                line(b, f.label.clone(), MUTED, 12.0);
                line(b, f.value.clone(), TEXT, 15.0);
            });
    }
    section(p, "Daily routine");
    if data.routine.slots.is_empty() {
        line(p, "  (no routine: they decide everything themselves)".into(), MUTED, 14.0);
    }
    for s in &data.routine.slots {
        let mark = if s.now { ">" } else { " " };
        let notes = if s.notes.is_empty() { String::new() } else { format!("  ({})", s.notes) };
        let done = if s.done { "  - done today" } else { "" };
        let color = if s.now { SAID } else if s.done { MUTED } else { TEXT };
        line(p, format!(" {mark} {}   {} - {}{notes}{done}", s.hours, s.r#where, s.what), color, 14.0);
    }
    line(p, "The routine is a default: news, requests and emergencies can make them do something else.".into(), MUTED, 12.0);
}

/// "  ·  party: Sage, Reed" (people travelling with the player).
fn party_text(world: &WorldSnapshot) -> String {
    let me = &world.player.name;
    let mut party: Vec<&str> = world
        .characters
        .iter()
        .filter(|c| c.follows == *me && c.name != *me)
        .map(|c| c.name.as_str())
        .collect();
    party.sort();
    if party.is_empty() {
        String::new()
    } else {
        format!("  ·  party: {}", party.join(", "))
    }
}

#[cfg(test)]
mod menu_tests {
    use super::*;

    #[test]
    fn similar_items_are_one_line() {
        let held: Vec<String> = ["Berry1", "Berry2", "Wood_1", "Key", "Berry3"].iter().map(|s| s.to_string()).collect();
        let groups = group_items(&held);
        let labels: Vec<String> = groups.iter().map(ItemGroup::label).collect();
        assert_eq!(labels, vec!["Berry x3", "Wood", "Key"]);
        assert_eq!(groups[0].first, "Berry1", "the first one stands in for the group");
    }

    #[test]
    fn names_that_are_only_digits_keep_their_name() {
        assert_eq!(item_base("42"), "42");
        assert_eq!(item_base("Lantern"), "Lantern");
        assert_eq!(item_base("Fish 12"), "Fish");
    }
}
