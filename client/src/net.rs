use bevy::prelude::*;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{Read, Write},
    net::TcpStream,
    sync::{
        mpsc::{self, Receiver, Sender},
        Mutex,
    },
    thread,
    time::{Duration, Instant},
};

use crate::ui::{DialogueView, UiState};

pub const SYNC_INTERVAL: Duration = Duration::from_millis(500);

#[derive(Resource, Clone)]
pub struct ClientConfig {
    pub server: String,
}

impl ClientConfig {
    pub fn from_args() -> Self {
        let mut server = "http://127.0.0.1:8765".to_string();
        let mut args = std::env::args().skip(1);
        while let Some(arg) = args.next() {
            if arg == "--server" {
                if let Some(value) = args.next() {
                    server = value;
                }
            }
        }
        Self { server }
    }
}

#[derive(Resource)]
pub struct NetBridge {
    tx: Sender<NetRequest>,
    rx: Mutex<Receiver<NetResponse>>,
    server: String,
    state_in_flight: bool,
    last_state_sync: Instant,
    pub last_message: String,
}

impl NetBridge {
    pub fn new(server: &str) -> Self {
        let (request_tx, request_rx) = mpsc::channel::<NetRequest>();
        let (response_tx, response_rx) = mpsc::channel::<NetResponse>();

        thread::spawn(move || {
            while let Ok(request) = request_rx.recv() {
                let response = handle_request(request);
                if response_tx.send(response).is_err() {
                    break;
                }
            }
        });

        let bridge = Self {
            tx: request_tx,
            rx: Mutex::new(response_rx),
            server: server.to_string(),
            state_in_flight: false,
            last_state_sync: Instant::now() - SYNC_INTERVAL,
            last_message: "Starting offline; waiting for AITown3 server.".to_string(),
        };
        bridge.send(NetRequestKind::SimulationStatus);
        bridge
    }

    fn send(&self, kind: NetRequestKind) {
        let _ = self.tx.send(NetRequest {
            server: self.server.clone(),
            kind,
        });
    }

    pub fn execute(&self, action: &str, params: HashMap<String, String>) {
        self.send(NetRequestKind::ExecuteAction {
            action: action.to_string(),
            params,
        });
    }

    pub fn set_simulation(&self, running: bool, use_llm: bool) {
        self.send(NetRequestKind::SetSimulation { running, use_llm });
    }

    pub fn advance_time(&self, hours: f32) {
        self.send(NetRequestKind::AdvanceTime { hours });
    }

    pub fn refresh_now(&mut self) {
        self.last_state_sync = Instant::now() - SYNC_INTERVAL;
    }

    pub fn fetch_history(&self, name: &str) {
        self.send(NetRequestKind::FetchHistory {
            name: name.to_string(),
        });
    }

    pub fn fetch_blueprints(&self) {
        self.send(NetRequestKind::FetchBlueprints);
    }

    pub fn build(&self, archetype: &str, x: i32, y: i32) {
        self.send(NetRequestKind::Build {
            archetype: archetype.to_string(),
            x,
            y,
        });
    }
}

enum NetRequestKind {
    FetchState,
    ExecuteAction {
        action: String,
        params: HashMap<String, String>,
    },
    SetSimulation {
        running: bool,
        use_llm: bool,
    },
    SimulationStatus,
    AdvanceTime {
        hours: f32,
    },
    FetchHistory {
        name: String,
    },
    FetchBlueprints,
    Build {
        archetype: String,
        x: i32,
        y: i32,
    },
}

struct NetRequest {
    server: String,
    kind: NetRequestKind,
}

enum NetResponse {
    State(Result<PlayState, String>),
    Action(Result<ActionResult, String>),
    Simulation(Result<bool, String>),
    Time(Result<String, String>),
    History(String, Result<History, String>),
    Blueprints(Result<Vec<Blueprint>, String>),
    Build(Result<(bool, String), String>),
}

fn handle_request(request: NetRequest) -> NetResponse {
    let server = &request.server;
    match request.kind {
        NetRequestKind::FetchState => match http_json(server, "GET", "/api/play/state", None) {
            Ok(value) => match serde_json::from_value::<PlayState>(value) {
                Ok(state) => NetResponse::State(Ok(state)),
                Err(err) => NetResponse::State(Err(format!("Bad state JSON: {err}"))),
            },
            Err(err) => NetResponse::State(Err(err)),
        },
        NetRequestKind::ExecuteAction { action, params } => {
            let body = json!({ "action": action, "params": params, "message": "" });
            match http_json(server, "POST", "/api/actions/execute", Some(body.to_string())) {
                Ok(value) => NetResponse::Action(Ok(ActionResult::from_json(&action, &value))),
                Err(err) => NetResponse::Action(Err(err)),
            }
        }
        NetRequestKind::SetSimulation { running, use_llm } => {
            let result = if running {
                let body = json!({ "interval_seconds": 0.25, "use_llm": use_llm });
                http_json(server, "POST", "/api/simulation/start", Some(body.to_string()))
            } else {
                http_json(server, "POST", "/api/simulation/stop", Some("{}".to_string()))
            };
            NetResponse::Simulation(result.map(|v| {
                v.get("running").and_then(Value::as_bool).unwrap_or(running)
            }))
        }
        NetRequestKind::SimulationStatus => {
            let result = http_json(server, "GET", "/api/simulation/status", None);
            NetResponse::Simulation(
                result.map(|v| v.get("running").and_then(Value::as_bool).unwrap_or(false)),
            )
        }
        NetRequestKind::AdvanceTime { hours } => {
            let body = json!({ "hours": hours });
            let result = http_json(server, "POST", "/api/time/advance", Some(body.to_string()));
            NetResponse::Time(result.map(|v| {
                v.get("clock")
                    .and_then(|c| c.get("text"))
                    .and_then(Value::as_str)
                    .unwrap_or("?")
                    .to_string()
            }))
        }
        NetRequestKind::FetchHistory { name } => {
            let path = format!("/api/characters/{name}/history?limit=80");
            let result = http_json(server, "GET", &path, None).and_then(|value| {
                serde_json::from_value::<History>(value).map_err(|err| format!("Bad history JSON: {err}"))
            });
            NetResponse::History(name, result)
        }
        NetRequestKind::FetchBlueprints => {
            let result = http_json(server, "GET", "/api/build/blueprints", None).and_then(|value| {
                serde_json::from_value::<Vec<Blueprint>>(
                    value.get("blueprints").cloned().unwrap_or(Value::Array(Vec::new())),
                )
                .map_err(|err| format!("Bad blueprints JSON: {err}"))
            });
            NetResponse::Blueprints(result)
        }
        NetRequestKind::Build { archetype, x, y } => {
            let body = json!({ "archetype": archetype, "x": x, "y": y });
            let result = http_json(server, "POST", "/api/build", Some(body.to_string())).map(|v| {
                (
                    v.get("success").and_then(Value::as_bool).unwrap_or(false),
                    v.get("message").and_then(Value::as_str).unwrap_or("").to_string(),
                )
            });
            NetResponse::Build(result)
        }
    }
}

#[derive(Deserialize, Clone, Default)]
pub struct Blueprint {
    #[serde(default, deserialize_with = "lenient_string")]
    pub archetype: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub label: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub description: String,
    #[serde(default = "one")]
    pub w: i32,
    #[serde(default = "one")]
    pub h: i32,
    #[serde(default)]
    pub available: bool,
    #[serde(default, deserialize_with = "lenient_string")]
    pub reason: String,
}

fn one() -> i32 {
    1
}

#[derive(Deserialize, Clone, Default)]
pub struct History {
    #[serde(default, deserialize_with = "lenient_string")]
    pub goal: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub goal_reason: String,
    #[serde(default, deserialize_with = "lenient_vec")]
    pub plan: Vec<String>,
    #[serde(default, deserialize_with = "lenient_string")]
    pub status: String,
    #[serde(default)]
    pub entries: Vec<HistoryEntry>,
}

#[derive(Deserialize, Clone, Default)]
pub struct HistoryEntry {
    #[serde(default, deserialize_with = "lenient_string")]
    pub clock: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub kind: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub text: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub reason: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub message: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub source: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub mode: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub step: String,
    #[serde(default, deserialize_with = "lenient_vec")]
    pub triggers: Vec<String>,
    #[serde(default, deserialize_with = "lenient_vec")]
    pub plan: Vec<String>,
    #[serde(default, deserialize_with = "lenient_vec")]
    pub dialogue: Vec<String>,
}

pub struct ActionResult {
    pub action: String,
    pub success: bool,
    pub message: String,
    pub lines: Vec<(String, String)>,
}

impl ActionResult {
    fn from_json(action: &str, value: &Value) -> Self {
        let message = value
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("Action complete")
            .to_string();
        let success = value.get("success").and_then(Value::as_bool).unwrap_or(true);
        let dialogue = value.get("dialogue");
        let mut lines = dialogue
            .and_then(|d| d.get("lines"))
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(|line| {
                        let speaker = line.get("speaker").and_then(Value::as_str)?.to_string();
                        let text = line.get("text").and_then(Value::as_str)?.to_string();
                        (!text.trim().is_empty()).then_some((speaker, text))
                    })
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        if lines.is_empty() {
            if let Some(err) = dialogue.and_then(|d| d.get("error")).and_then(Value::as_str) {
                lines.push((String::new(), format!("(dialogue failed: {err})")));
            }
        }
        Self {
            action: action.to_string(),
            success,
            message,
            lines,
        }
    }
}

pub fn schedule_state_sync(mut net: ResMut<NetBridge>) {
    if net.state_in_flight || net.last_state_sync.elapsed() < SYNC_INTERVAL {
        return;
    }
    net.state_in_flight = true;
    net.last_state_sync = Instant::now();
    net.send(NetRequestKind::FetchState);
}

pub fn receive_network_messages(
    mut net: ResMut<NetBridge>,
    mut world: ResMut<WorldSnapshot>,
    mut ui: ResMut<UiState>,
) {
    let responses = {
        let rx = net.rx.lock().expect("network response receiver poisoned");
        let mut responses = Vec::new();
        while let Ok(response) = rx.try_recv() {
            responses.push(response);
        }
        responses
    };

    for response in responses {
        match response {
            NetResponse::State(Ok(state)) => {
                let mut next = WorldSnapshot::from(state);
                next.revision = world.revision + 1;
                let latest = next.simulation.chronicle.first().map(|e| e.text.clone());
                let previous = world.simulation.chronicle.first().map(|e| e.text.clone());
                if world.revision > 0 && latest.is_some() && latest != previous {
                    ui.toast(format!("News: {}", latest.unwrap_or_default()));
                }
                *world = next;
                net.state_in_flight = false;
                net.last_message = "Synced with AITown3.".to_string();
            }
            NetResponse::State(Err(err)) => {
                net.state_in_flight = false;
                net.last_message = format!("Offline: {err}");
                if world.online {
                    world.online = false;
                }
            }
            NetResponse::Action(Ok(result)) => {
                net.last_message = result.message.clone();
                net.refresh_now();
                if result.action == "talk" && !result.success {
                    ui.dialogue = None;
                    ui.toast(format!("Can't talk: {}", result.message));
                } else if result.action == "talk" {
                    let partner = ui
                        .dialogue
                        .as_ref()
                        .map(|d| d.partner.clone())
                        .unwrap_or_default();
                    let lines = if result.lines.is_empty() {
                        vec![(String::new(), result.message.clone())]
                    } else {
                        result.lines
                    };
                    ui.dialogue = Some(DialogueView {
                        partner,
                        lines,
                        waiting: false,
                    });
                } else if result.success {
                    if !ui.travelling() {
                        ui.toast(result.message);
                    }
                } else {
                    ui.cancel_travel();
                    ui.toast(format!("Can't do that: {}", result.message));
                }
            }
            NetResponse::Blueprints(Ok(list)) => ui.open_build_menu(list),
            NetResponse::Blueprints(Err(err)) => ui.toast(format!("Can't load blueprints: {err}")),
            NetResponse::Build(Ok((success, message))) => {
                net.refresh_now();
                if success {
                    ui.build = None;
                    ui.toast(message);
                } else {
                    ui.toast(format!("Can't build: {message}"));
                }
            }
            NetResponse::Build(Err(err)) => ui.toast(format!("Build failed: {err}")),
            NetResponse::Action(Err(err)) => {
                net.last_message = format!("Action failed: {err}");
                net.refresh_now();
                ui.cancel_travel();
                if ui.dialogue.as_ref().is_some_and(|d| d.waiting) {
                    ui.dialogue = None;
                }
                ui.toast(format!("Action failed: {err}"));
            }
            NetResponse::Simulation(Ok(running)) => {
                ui.sim_running = running;
            }
            NetResponse::Simulation(Err(err)) => {
                ui.toast(format!("Simulation request failed: {err}"));
            }
            NetResponse::Time(Ok(clock)) => {
                net.refresh_now();
                ui.toast(format!("Time skipped -> {clock}"));
            }
            NetResponse::Time(Err(err)) => {
                ui.toast(format!("Time skip failed: {err}"));
            }
            NetResponse::History(name, result) => {
                if let Some(view) = ui.history.as_mut().filter(|v| v.name == name) {
                    view.in_flight = false;
                    match result {
                        Ok(data) => {
                            view.data = Some(data);
                            view.error.clear();
                        }
                        Err(err) => view.error = err,
                    }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// World snapshot (what the client knows about the server world)
// ---------------------------------------------------------------------------

#[derive(Resource, Clone)]
pub struct WorldSnapshot {
    pub found: bool,
    pub player: Actor,
    pub current_location: Location,
    pub adjacent: Vec<String>,
    pub inventory: Vec<String>,
    pub people_here: Vec<Actor>,
    pub items_here: Vec<Thing>,
    pub use_on_by_target: HashMap<String, Vec<String>>,
    pub characters: Vec<Actor>,
    pub simulation: Simulation,
    pub map: WorldMap,
    pub online: bool,
    pub revision: u64,
}

impl WorldSnapshot {
    pub fn offline() -> Self {
        Self {
            found: true,
            player: Actor {
                name: "Traveler".to_string(),
                location: Some("Town Square".to_string()),
                hp: 100,
                alive: true,
                role: "player".to_string(),
                ..default()
            },
            current_location: Location {
                name: "Town Square".to_string(),
                description: "Offline Bevy shell. Start the Python server to sync real AITown3 state."
                    .to_string(),
                ..default()
            },
            adjacent: vec!["Clinic".to_string(), "Forest Path".to_string()],
            inventory: Vec::new(),
            people_here: vec![Actor {
                name: "Mira".to_string(),
                location: Some("Town Square".to_string()),
                hp: 100,
                alive: true,
                role: "npc".to_string(),
                ..default()
            }],
            items_here: vec![Thing {
                name: "Old key".to_string(),
                portable: true,
                description: "A demo item".to_string(),
                ..default()
            }],
            use_on_by_target: HashMap::new(),
            characters: Vec::new(),
            simulation: Simulation::default(),
            map: WorldMap::default(),
            online: false,
            revision: 0,
        }
    }

    pub fn held_items(&self) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        for item in self.inventory.iter().chain(self.player.held_items.iter()) {
            if !item.is_empty() && !out.contains(item) {
                out.push(item.clone());
            }
        }
        out
    }

    pub fn person_here(&self, name: &str) -> Option<&Actor> {
        self.people_here.iter().find(|p| p.name == name)
    }

    pub fn character(&self, name: &str) -> Option<&Actor> {
        self.characters.iter().find(|p| p.name == name)
    }

    pub fn activity(&self, name: &str) -> Option<&NpcActivity> {
        self.simulation.npc_activity.iter().find(|a| a.name == name)
    }

    pub fn recommended_tools(&self, target: &str) -> &[String] {
        self.use_on_by_target
            .get(target)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }
}

impl From<PlayState> for WorldSnapshot {
    fn from(state: PlayState) -> Self {
        if !state.found {
            let mut offline = WorldSnapshot::offline();
            offline.found = false;
            offline.online = true;
            offline.current_location.description = state.message.unwrap_or_else(|| {
                "Server reachable, but no player exists. Load a template world.".to_string()
            });
            return offline;
        }

        let here = state.here.unwrap_or_default();
        let player = state.player.unwrap_or_default();
        let current_location = state.current_location.unwrap_or_else(|| Location {
            name: player
                .location
                .clone()
                .unwrap_or_else(|| "Unknown location".to_string()),
            ..default()
        });
        let mut use_on_by_target = HashMap::new();
        if let Some(map) = state.use_on_by_target.as_object() {
            for (target, entry) in map {
                let tools = entry
                    .get("tools")
                    .and_then(Value::as_array)
                    .map(|tools| {
                        tools
                            .iter()
                            .filter_map(Value::as_str)
                            .map(str::to_string)
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_default();
                use_on_by_target.insert(target.clone(), tools);
            }
        }
        let player_name = player.name.clone();

        Self {
            found: true,
            player,
            current_location,
            adjacent: state.adjacent.unwrap_or_default(),
            inventory: state.inventory.unwrap_or_default(),
            people_here: here
                .persons
                .into_iter()
                .filter(|p| p.name != player_name)
                .collect(),
            items_here: here
                .items
                .into_iter()
                .chain(here.interactables)
                .collect::<Vec<_>>(),
            use_on_by_target,
            characters: state.characters.unwrap_or_default(),
            simulation: state.simulation.unwrap_or_default(),
            map: state.map.unwrap_or_default(),
            online: true,
            revision: 0,
        }
    }
}

#[derive(Deserialize)]
struct PlayState {
    #[serde(default)]
    found: bool,
    #[serde(default)]
    message: Option<String>,
    #[serde(default)]
    player: Option<Actor>,
    #[serde(default)]
    current_location: Option<Location>,
    #[serde(default)]
    adjacent: Option<Vec<String>>,
    #[serde(default)]
    here: Option<Here>,
    #[serde(default)]
    inventory: Option<Vec<String>>,
    #[serde(default)]
    use_on_by_target: Value,
    #[serde(default)]
    characters: Option<Vec<Actor>>,
    #[serde(default)]
    simulation: Option<Simulation>,
    #[serde(default)]
    map: Option<WorldMap>,
}

#[derive(Deserialize, Clone, Default)]
pub struct Actor {
    #[serde(default, deserialize_with = "lenient_string")]
    pub name: String,
    #[serde(default)]
    pub location: Option<String>,
    #[serde(default, deserialize_with = "lenient_i32")]
    pub hp: i32,
    #[serde(default)]
    pub alive: bool,
    #[serde(default, deserialize_with = "lenient_string")]
    pub role: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub mood: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub personality: String,
    #[serde(default, deserialize_with = "lenient_vec")]
    pub held_items: Vec<String>,
}

#[derive(Deserialize, Clone, Default)]
pub struct Thing {
    #[serde(default, deserialize_with = "lenient_string")]
    pub name: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub item_kind: String,
    #[serde(default)]
    pub locked: bool,
    #[serde(default = "default_true")]
    pub portable: bool,
    #[serde(default, deserialize_with = "lenient_string")]
    pub description: String,
    /// Grid cell of the top-left corner in the current zone (None = not placed).
    #[serde(default)]
    pub x: Option<i32>,
    #[serde(default)]
    pub y: Option<i32>,
    #[serde(default = "one")]
    pub w: i32,
    #[serde(default = "one")]
    pub h: i32,
}

impl Thing {
    pub fn cell(&self) -> Option<IVec2> {
        Some(IVec2::new(self.x?, self.y?))
    }

    pub fn footprint(&self) -> IVec2 {
        IVec2::new(self.w.max(1), self.h.max(1))
    }

    pub fn is_door(&self) -> bool {
        self.item_kind.eq_ignore_ascii_case("door")
    }

    pub fn is_fixture(&self) -> bool {
        self.is_door() || !self.portable
    }

    pub fn can_pick_up(&self) -> bool {
        self.portable && !self.locked && !self.is_fixture()
    }
}

#[derive(Deserialize, Clone, Default)]
pub struct Location {
    #[serde(default, deserialize_with = "lenient_string")]
    pub name: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub description: String,
    #[serde(default)]
    pub grid_w: i32,
    #[serde(default)]
    pub grid_h: i32,
}

/// Zones and routes for the world map.
#[derive(Deserialize, Clone, Default)]
pub struct WorldMap {
    #[serde(default)]
    pub locations: Vec<MapZone>,
    #[serde(default)]
    pub edges: Vec<MapEdge>,
}

#[derive(Deserialize, Clone, Default)]
pub struct MapZone {
    #[serde(default, deserialize_with = "lenient_string")]
    pub name: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub description: String,
    #[serde(default)]
    pub map_x: Option<f32>,
    #[serde(default)]
    pub map_y: Option<f32>,
    #[serde(default, deserialize_with = "lenient_vec")]
    pub persons: Vec<String>,
    #[serde(default, deserialize_with = "lenient_vec")]
    pub adjacent: Vec<String>,
}

#[derive(Deserialize, Clone, Default)]
pub struct MapEdge {
    #[serde(default, deserialize_with = "lenient_string")]
    pub a: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub b: String,
}

impl WorldMap {
    /// Shortest hop list from `from` to `to` (excluding `from`).
    pub fn route(&self, from: &str, to: &str) -> Option<Vec<String>> {
        if from == to {
            return Some(Vec::new());
        }
        let mut prev: HashMap<String, String> = HashMap::new();
        let mut queue = std::collections::VecDeque::from([from.to_string()]);
        prev.insert(from.to_string(), String::new());
        while let Some(cur) = queue.pop_front() {
            for next in self.neighbours(&cur) {
                if prev.contains_key(&next) {
                    continue;
                }
                prev.insert(next.clone(), cur.clone());
                if next == to {
                    let mut path = vec![next];
                    while let Some(p) = prev.get(path.last().unwrap()).filter(|p| !p.is_empty() && *p != from) {
                        path.push(p.clone());
                    }
                    path.reverse();
                    return Some(path);
                }
                queue.push_back(next);
            }
        }
        None
    }

    fn neighbours(&self, zone: &str) -> Vec<String> {
        let mut out: Vec<String> = self
            .locations
            .iter()
            .find(|z| z.name == zone)
            .map(|z| z.adjacent.clone())
            .unwrap_or_default();
        for e in &self.edges {
            if e.a == zone && !out.contains(&e.b) {
                out.push(e.b.clone());
            } else if e.b == zone && !out.contains(&e.a) {
                out.push(e.a.clone());
            }
        }
        out
    }
}

#[derive(Deserialize, Clone, Default)]
struct Here {
    #[serde(default)]
    persons: Vec<Actor>,
    #[serde(default)]
    items: Vec<Thing>,
    #[serde(default)]
    interactables: Vec<Thing>,
}

#[derive(Deserialize, Clone, Default)]
pub struct Simulation {
    #[serde(default)]
    pub tick: u64,
    #[serde(default)]
    pub clock: Clock,
    #[serde(default)]
    pub npc_activity: Vec<NpcActivity>,
    /// World announcements, newest first.
    #[serde(default)]
    pub chronicle: Vec<ChronicleEntry>,
}

#[derive(Deserialize, Clone, Default)]
pub struct ChronicleEntry {
    #[serde(default, deserialize_with = "lenient_string")]
    pub clock: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub text: String,
}

#[derive(Deserialize, Clone, Default)]
pub struct Clock {
    #[serde(default, deserialize_with = "lenient_string")]
    pub text: String,
    #[serde(default)]
    pub hour: f32,
    #[serde(default)]
    pub day: u32,
    #[serde(default, deserialize_with = "lenient_string")]
    pub phase_label: String,
}

#[derive(Deserialize, Clone, Default)]
pub struct NpcActivity {
    #[serde(default, deserialize_with = "lenient_string")]
    pub name: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub status: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub pending_action: String,
    #[serde(default, deserialize_with = "lenient_vec")]
    pub pending_args: Vec<String>,
    #[serde(default, deserialize_with = "lenient_string")]
    pub reason: String,
    /// 持续状态（内容包 states/，如 asleep）；空 = 无
    #[serde(default, deserialize_with = "lenient_string")]
    pub state: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub state_label: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub trigger: String,
}

fn default_true() -> bool {
    true
}

fn value_to_string(value: Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(s) => s,
        other => other.to_string(),
    }
}

fn lenient_string<'de, D: serde::Deserializer<'de>>(de: D) -> Result<String, D::Error> {
    Ok(value_to_string(Value::deserialize(de)?))
}

fn lenient_i32<'de, D: serde::Deserializer<'de>>(de: D) -> Result<i32, D::Error> {
    Ok(Value::deserialize(de)?.as_f64().map(|v| v as i32).unwrap_or(0))
}

fn lenient_vec<'de, D: serde::Deserializer<'de>>(de: D) -> Result<Vec<String>, D::Error> {
    Ok(match Value::deserialize(de)? {
        Value::Array(items) => items
            .into_iter()
            .map(value_to_string)
            .filter(|s| !s.is_empty())
            .collect(),
        _ => Vec::new(),
    })
}

pub fn trim_line(value: &str, max_chars: usize) -> String {
    let mut out = value.replace('\n', " ");
    if out.chars().count() > max_chars {
        out = out.chars().take(max_chars.saturating_sub(3)).collect::<String>();
        out.push_str("...");
    }
    out
}

fn http_json(
    base_url: &str,
    method: &str,
    path: &str,
    body: Option<String>,
) -> Result<Value, String> {
    let base = base_url
        .strip_prefix("http://")
        .ok_or_else(|| "Only http:// servers are supported in this minimal client".to_string())?;
    let (host_port, prefix) = base.split_once('/').unwrap_or((base, ""));
    let (host, port) = host_port
        .split_once(':')
        .map(|(h, p)| (h, p.parse::<u16>().unwrap_or(80)))
        .unwrap_or((host_port, 80));
    let full_path = if prefix.is_empty() {
        path.to_string()
    } else {
        format!("/{prefix}{path}")
    };

    let mut stream = TcpStream::connect((host, port))
        .map_err(|err| format!("Could not connect to {host}:{port}: {err}"))?;
    let read_timeout = if method == "POST" { 90 } else { 15 };
    stream
        .set_read_timeout(Some(Duration::from_secs(read_timeout)))
        .map_err(|err| err.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .map_err(|err| err.to_string())?;

    let body = body.unwrap_or_default();
    let request = if method == "POST" {
        format!(
            "POST {full_path} HTTP/1.1\r\nHost: {host_port}\r\nContent-Type: application/json\r\nAccept: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
    } else {
        format!(
            "GET {full_path} HTTP/1.1\r\nHost: {host_port}\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
        )
    };
    stream
        .write_all(request.as_bytes())
        .map_err(|err| err.to_string())?;

    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|err| err.to_string())?;
    let (head, body) = response
        .split_once("\r\n\r\n")
        .ok_or_else(|| "Malformed HTTP response".to_string())?;
    if !head.starts_with("HTTP/1.1 200") && !head.starts_with("HTTP/1.0 200") {
        let status = head.lines().next().unwrap_or("HTTP error");
        let detail = serde_json::from_str::<Value>(body)
            .ok()
            .and_then(|v| v.get("detail").and_then(Value::as_str).map(str::to_string))
            .unwrap_or_default();
        if detail.contains("7687") {
            return Err(
                "Server cannot reach Neo4j (localhost:7687). Start Neo4j, then press R.".to_string(),
            );
        }
        if detail.is_empty() {
            return Err(status.to_string());
        }
        return Err(format!("{status}: {}", trim_line(&detail, 160)));
    }
    serde_json::from_str(body).map_err(|err| format!("Invalid JSON response: {err}"))
}
