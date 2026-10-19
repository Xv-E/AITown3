use bevy::prelude::*;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{BufRead, BufReader, Read, Write},
    net::TcpStream,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, Sender},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

use crate::ui::{DialogueView, UiState};

pub const SYNC_INTERVAL: Duration = Duration::from_millis(500);
/// Server keepalives arrive every 15 s; a silent stream for longer than this is dead.
const STREAM_READ_TIMEOUT: Duration = Duration::from_secs(40);
const STREAM_MAX_BACKOFF: Duration = Duration::from_secs(5);

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
    /// True while the `/api/stream` push channel is connected; polling pauses meanwhile.
    streaming: Arc<AtomicBool>,
    /// Player position reports go on their own thread (latest wins), so a slow request
    /// such as an LLM dialogue never holds them up.
    position_tx: Sender<IVec2>,
    pub last_message: String,
}

impl NetBridge {
    pub fn new(server: &str) -> Self {
        let (request_tx, request_rx) = mpsc::channel::<NetRequest>();
        let (response_tx, response_rx) = mpsc::channel::<NetResponse>();
        let streaming = Arc::new(AtomicBool::new(false));
        spawn_state_stream(server.to_string(), response_tx.clone(), streaming.clone());
        let position_tx = spawn_position_reporter(server.to_string());

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
            streaming,
            position_tx,
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
            message: String::new(),
            history: Vec::new(),
        });
    }

    /// Say something in a conversation (`history`: earlier lines of this chat, oldest first).
    pub fn say(&self, params: HashMap<String, String>, message: &str, history: &[(String, String)]) {
        self.send(NetRequestKind::ExecuteAction {
            action: "talk".to_string(),
            params,
            message: message.to_string(),
            history: history.to_vec(),
        });
    }

    /// The player closed their chat with `npc`: it no longer has to stand and wait.
    pub fn chat_end(&self, player: &str, npc: &str) {
        self.send(NetRequestKind::ChatEnd { player: player.to_string(), npc: npc.to_string() });
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

    pub fn is_streaming(&self) -> bool {
        self.streaming.load(Ordering::Relaxed)
    }

    /// Tell the server which world cell the player stands on (it updates their region).
    pub fn report_position(&self, cell: IVec2) {
        let _ = self.position_tx.send(cell);
    }

    /// Make `name` an LLM character ("llm") or a background NPC that only follows its routine ("routine").
    pub fn set_brain(&self, name: &str, brain: &str) {
        self.send(NetRequestKind::SetBrain { name: name.to_string(), brain: brain.to_string() });
    }

    /// Invite `npc` to travel with you (join = true) or ask them to go (false).
    pub fn party(&self, npc: &str, join: bool) {
        self.send(NetRequestKind::Social { path: format!("/api/party/{}?join={join}", url_encode(npc)) });
    }

    pub fn propose_alliance(&self, npc: &str) {
        self.send(NetRequestKind::Social { path: format!("/api/ally/{}", url_encode(npc)) });
    }

    pub fn fetch_requests(&self) {
        self.send(NetRequestKind::FetchRequests);
    }

    /// Hand the thing a request asks for to whoever posted it.
    pub fn deliver(&self, request_id: &str) {
        self.send(NetRequestKind::Social { path: format!("/api/requests/{}/deliver", url_encode(request_id)) });
    }

    pub fn fetch_crafting(&self) {
        self.send(NetRequestKind::FetchCrafting);
    }

    pub fn fetch_trade(&self, npc: &str) {
        self.send(NetRequestKind::FetchTrade { npc: npc.to_string() });
    }

    /// Gather / craft / buy / sell: the server picks which materials, tool or coins to use.
    pub fn do_action(&self, action: &str, target: &str, other: &str) {
        self.send(NetRequestKind::Do { action: action.to_string(), target: target.to_string(), other: other.to_string() });
    }

    pub fn fetch_saves(&self) {
        self.send(NetRequestKind::FetchSaves);
    }

    pub fn save_game(&self, slot: &str) {
        self.send(NetRequestKind::SaveGame { slot: slot.to_string() });
    }

    /// Replace the whole world (and NPC progress / memories) with a save.
    pub fn load_game(&self, slot: &str) {
        self.send(NetRequestKind::LoadGame { slot: slot.to_string() });
    }

    pub fn fetch_director(&self) {
        self.send(NetRequestKind::FetchDirector);
    }

    /// Ask the storyteller to act on the next tick instead of waiting.
    pub fn nudge_director(&self) {
        self.send(NetRequestKind::NudgeDirector);
    }

    /// Have the LLM write a questline and check it with the planner (slow: up to a few minutes).
    /// `theme`: what the player wants it to be about ("" = let the designer pick).
    pub fn draft_questline(&self, theme: &str) {
        self.send(NetRequestKind::DraftQuestline { theme: theme.to_string() });
    }

    pub fn apply_questline(&self) {
        self.send(NetRequestKind::ApplyQuestline);
    }

    /// Memories (optionally one type, or searched by meaning) and what `name` knows.
    pub fn fetch_mind(&self, name: &str, query: &str, kind: &str) {
        self.send(NetRequestKind::FetchMind { name: name.to_string(), query: query.to_string(), kind: kind.to_string() });
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
        message: String,
        history: Vec<(String, String)>,
    },
    SetSimulation {
        running: bool,
        use_llm: bool,
    },
    SimulationStatus,
    ChatEnd {
        player: String,
        npc: String,
    },
    AdvanceTime {
        hours: f32,
    },
    FetchMind {
        name: String,
        query: String,
        kind: String,
    },
    FetchSaves,
    FetchCrafting,
    FetchRequests,
    Social {
        path: String,
    },
    FetchTrade {
        npc: String,
    },
    Do {
        action: String,
        target: String,
        other: String,
    },
    SetBrain {
        name: String,
        brain: String,
    },
    SaveGame {
        slot: String,
    },
    LoadGame {
        slot: String,
    },
    FetchDirector,
    NudgeDirector,
    DraftQuestline {
        theme: String,
    },
    ApplyQuestline,
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
    /// Fire-and-forget requests: nothing to show.
    Done,
    Director(Result<DirectorData, String>),
    Saves(Result<Vec<SaveInfo>, String>),
    Mind(String, Result<MindData, String>),
    Crafting(Result<Vec<RecipeView>, String>),
    Requests(Result<Vec<RequestView>, String>),
    Trade(Result<TradeView, String>),
    Did(String, Result<(bool, String), String>),
    Saved(Result<String, String>),
    Loaded(Result<String, String>),
    Draft(Result<DraftSummary, String>),
    Applied(Result<String, String>),
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
        NetRequestKind::ExecuteAction { action, params, message, history } => {
            let history: Vec<_> = history
                .iter()
                .filter(|(who, _)| !who.is_empty())
                .map(|(who, text)| json!({ "speaker": who, "text": text }))
                .collect();
            let body = json!({ "action": action, "params": params, "message": message, "history": history });
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
        NetRequestKind::ChatEnd { player, npc } => {
            let body = json!({ "player": player, "npc": npc });
            let _ = http_json(server, "POST", "/api/player/chat_end", Some(body.to_string()));
            NetResponse::Done
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
        NetRequestKind::FetchMind { name, query, kind } => {
            let path = format!(
                "/api/characters/{}/mind?query={}&kind={}&limit=80",
                url_encode(&name),
                url_encode(&query),
                url_encode(&kind)
            );
            let result = http_json(server, "GET", &path, None)
                .and_then(|v| serde_json::from_value::<MindData>(v).map_err(|err| format!("Bad memory JSON: {err}")));
            NetResponse::Mind(name, result)
        }
        NetRequestKind::SetBrain { name, brain } => NetResponse::Saved(
            http_json(
                server,
                "POST",
                &format!("/api/characters/{}/brain", url_encode(&name)),
                Some(json!({ "brain": brain }).to_string()),
            )
            .map(|v| v.get("message").and_then(Value::as_str).unwrap_or("Changed.").to_string()),
        ),
        NetRequestKind::FetchRequests => NetResponse::Requests(
            http_json(server, "GET", "/api/requests", None).and_then(|v| {
                serde_json::from_value::<Vec<RequestView>>(v.get("requests").cloned().unwrap_or(Value::Array(Vec::new())))
                    .map_err(|err| format!("Bad requests JSON: {err}"))
            }),
        ),
        NetRequestKind::Social { path } => NetResponse::Did(
            "social".into(),
            http_json(server, "POST", &path, Some("{}".to_string())).map(|v| {
                (
                    v.get("success").and_then(Value::as_bool).unwrap_or(false),
                    v.get("message").and_then(Value::as_str).unwrap_or("").to_string(),
                )
            }),
        ),
        NetRequestKind::FetchCrafting => NetResponse::Crafting(
            http_json(server, "GET", "/api/crafting", None).and_then(|v| {
                serde_json::from_value::<Vec<RecipeView>>(v.get("recipes").cloned().unwrap_or(Value::Array(Vec::new())))
                    .map_err(|err| format!("Bad crafting JSON: {err}"))
            }),
        ),
        NetRequestKind::FetchTrade { npc } => NetResponse::Trade(
            http_json(server, "GET", &format!("/api/trade/{}", url_encode(&npc)), None)
                .and_then(|v| serde_json::from_value::<TradeView>(v).map_err(|err| format!("Bad trade JSON: {err}"))),
        ),
        NetRequestKind::Do { action, target, other } => {
            let body = json!({ "action": action, "target": target, "other": other });
            let result = http_json(server, "POST", "/api/do", Some(body.to_string())).map(|v| {
                (
                    v.get("success").and_then(Value::as_bool).unwrap_or(false),
                    v.get("message").and_then(Value::as_str).unwrap_or("").to_string(),
                )
            });
            NetResponse::Did(action, result)
        }
        NetRequestKind::FetchSaves => NetResponse::Saves(
            http_json(server, "GET", "/api/saves", None).and_then(|v| {
                serde_json::from_value::<Vec<SaveInfo>>(v.get("saves").cloned().unwrap_or(Value::Array(Vec::new())))
                    .map_err(|err| format!("Bad saves JSON: {err}"))
            }),
        ),
        NetRequestKind::SaveGame { slot } => NetResponse::Saved(
            http_json(server, "POST", &format!("/api/saves/{slot}"), Some("{}".to_string()))
                .map(|v| v.get("message").and_then(Value::as_str).unwrap_or("Saved.").to_string()),
        ),
        NetRequestKind::LoadGame { slot } => NetResponse::Loaded(
            http_json(server, "POST", &format!("/api/saves/{slot}/load"), Some("{}".to_string()))
                .map(|v| v.get("message").and_then(Value::as_str).unwrap_or("Loaded.").to_string()),
        ),
        NetRequestKind::FetchDirector => NetResponse::Director(
            http_json(server, "GET", "/api/director", None).and_then(|v| {
                serde_json::from_value::<DirectorData>(v).map_err(|err| format!("Bad director JSON: {err}"))
            }),
        ),
        NetRequestKind::NudgeDirector => {
            let _ = http_json(server, "POST", "/api/director/nudge", Some("{}".to_string()));
            NetResponse::Done
        }
        NetRequestKind::DraftQuestline { theme } => NetResponse::Draft(
            http_json(
                server,
                "POST",
                "/api/questlines/draft",
                Some(json!({ "theme": theme, "complete": true }).to_string()),
            )
            .and_then(|v| {
                serde_json::from_value::<DraftSummary>(v.get("summary").cloned().unwrap_or(Value::Null))
                    .map_err(|err| format!("Bad draft JSON: {err}"))
            }),
        ),
        NetRequestKind::ApplyQuestline => NetResponse::Applied(
            http_json(server, "POST", "/api/questlines/apply", Some("{}".to_string())).map(|v| {
                v.get("message").and_then(Value::as_str).unwrap_or("Questline added to the world.").to_string()
            }),
        ),
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
    /// The goal in plain words ("feed the campfire").
    #[serde(default, deserialize_with = "lenient_string")]
    pub goal_line: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub goal_reason: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub status: String,
    /// The whole current plan, each step marked done / current / todo.
    #[serde(default)]
    pub steps: Vec<PlanStep>,
    /// 1-based step being worked on (0 = no plan).
    #[serde(default)]
    pub step: usize,
    #[serde(default)]
    pub now: PlanNow,
    #[serde(default)]
    pub entries: Vec<HistoryEntry>,
}

#[derive(Deserialize, Clone, Default)]
pub struct PlanStep {
    #[serde(default, deserialize_with = "lenient_string")]
    pub line: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub text: String,
    /// "done" | "current" | "todo"
    #[serde(default, deserialize_with = "lenient_string")]
    pub state: String,
}

/// What the NPC is doing this moment, in words, and how far along.
#[derive(Deserialize, Clone, Default)]
pub struct PlanNow {
    #[serde(default, deserialize_with = "lenient_string")]
    pub label: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub phase: String,
    #[serde(default)]
    pub progress: Option<f32>,
}

#[derive(Deserialize, Clone, Default)]
pub struct HistoryEntry {
    #[serde(default, deserialize_with = "lenient_string")]
    pub clock: String,
    /// The goal this entry served ("get Wood1"), and why it was chosen at the time.
    #[serde(default, rename = "for", deserialize_with = "lenient_string")]
    pub for_goal: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub why: String,
    /// The event as one plain sentence (story view).
    #[serde(default, deserialize_with = "lenient_string")]
    pub line: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub note: String,
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
    // The server pushes every change while the stream is up; poll only as a fallback.
    if net.is_streaming() || net.state_in_flight || net.last_state_sync.elapsed() < SYNC_INTERVAL {
        return;
    }
    net.state_in_flight = true;
    net.last_state_sync = Instant::now();
    net.send(NetRequestKind::FetchState);
}

pub fn receive_network_messages(
    mut net: ResMut<NetBridge>,
    mut world: ResMut<WorldSnapshot>,
    mut clock: ResMut<SimClock>,
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
                clock.observe(&next.simulation);
                *world = next;
                net.state_in_flight = false;
                net.last_message = if net.is_streaming() {
                    "Live (server push).".to_string()
                } else {
                    "Synced with AITown3.".to_string()
                };
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
                if result.action == "talk" && ui.dialogue.as_ref().is_some_and(|d| d.chat) {
                    // Typed chat: add their reply under what we said.
                    let me = world.player.name.clone();
                    if let Some(view) = ui.dialogue.as_mut() {
                        view.waiting = false;
                        if !result.success {
                            view.lines.push((String::new(), format!("(can't talk: {})", result.message)));
                        } else if result.lines.iter().all(|(who, _)| who.is_empty() || *who == me) {
                            view.lines.push((String::new(), format!("({})", result.message)));
                        } else {
                            view.lines.extend(result.lines.into_iter().filter(|(who, _)| *who != me));
                        }
                    }
                } else if result.action == "talk" && !result.success {
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
                    ui.dialogue = Some(DialogueView::shown(partner, lines));
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
            NetResponse::Saves(Ok(list)) => ui.open_save_menu(list),
            NetResponse::Crafting(Ok(list)) => ui.open_craft_menu(list),
            NetResponse::Requests(Ok(list)) => ui.open_requests_menu(list),
            NetResponse::Requests(Err(err)) => ui.toast(format!("Can't load requests: {err}")),
            NetResponse::Crafting(Err(err)) => ui.toast(format!("Can't load recipes: {err}")),
            NetResponse::Trade(Ok(view)) => ui.open_trade_menu(view),
            NetResponse::Trade(Err(err)) => ui.toast(format!("Can't trade: {err}")),
            NetResponse::Did(action, result) => {
                net.refresh_now();
                match result {
                    Ok((true, msg)) => {
                        ui.toast(msg);
                        // Keep the shop / workshop open with fresh numbers.
                        if (action == "buy" || action == "sell") && ui.menu.is_some() {
                            if let Some(npc) = ui.trading_with.clone() {
                                net.fetch_trade(&npc);
                            }
                        } else if action.starts_with("craft_") && ui.crafting_open && ui.menu.is_some() {
                            net.fetch_crafting();
                        }
                    }
                    Ok((false, msg)) => ui.toast(format!("Can't do that: {msg}")),
                    Err(err) => ui.toast(format!("Failed: {err}")),
                }
            }
            NetResponse::Mind(name, result) => {
                if let Some(view) = ui.history.as_mut().filter(|v| v.name == name) {
                    view.mind_in_flight = false;
                    match result {
                        Ok(data) => {
                            view.mind = Some(data);
                            view.mind_error.clear();
                        }
                        Err(err) => view.mind_error = err,
                    }
                }
            }
            NetResponse::Saves(Err(err)) => ui.toast(format!("Can't list saves: {err}")),
            NetResponse::Saved(result) => ui.toast(match result {
                Ok(msg) => msg,
                Err(err) => format!("Failed: {err}"),
            }),
            NetResponse::Loaded(result) => {
                net.refresh_now();
                ui.sim_running = false;
                ui.toast(match result {
                    Ok(msg) => format!("{msg} - NPCs are paused; start them from the bar."),
                    Err(err) => format!("Load failed: {err}"),
                });
            }
            NetResponse::Done => {}
            NetResponse::Director(result) => {
                if let Some(view) = ui.director.as_mut() {
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
            NetResponse::Draft(result) => {
                if let Some(view) = ui.director.as_mut() {
                    view.drafting = false;
                    match result {
                        Ok(draft) => view.draft = Some(draft),
                        Err(err) => view.error = format!("Drafting failed: {err}"),
                    }
                }
            }
            NetResponse::Applied(result) => {
                if let Some(view) = ui.director.as_mut() {
                    view.applying = false;
                    view.last_fetch = None;
                    if result.is_ok() {
                        view.draft = None;
                    }
                }
                match result {
                    Ok(msg) => ui.toast(msg),
                    Err(err) => ui.toast(format!("Couldn't add the questline: {err}")),
                }
            }
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
    /// The seamless world: regions, everything on the ground and everyone, in world cells.
    pub world: WorldView,
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
            world: WorldView::default(),
            online: false,
            revision: 0,
        }
    }

    /// Where the server last put the player (world cell).
    pub fn player_cell(&self) -> Option<IVec2> {
        self.world
            .persons
            .iter()
            .find(|p| p.name == self.player.name)
            .and_then(Actor::cell)
    }

    pub fn region(&self, name: &str) -> Option<&RegionView> {
        self.world.regions.iter().find(|r| r.name == name)
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
            world: state.world.unwrap_or_default(),
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
    world: Option<WorldView>,
}

#[derive(Deserialize, Clone, Default)]
pub struct WorldView {
    #[serde(default)]
    pub width: i32,
    #[serde(default)]
    pub height: i32,
    #[serde(default)]
    pub regions: Vec<RegionView>,
    /// Everything on the ground; x / y are world cells.
    #[serde(default)]
    pub items: Vec<Thing>,
    #[serde(default)]
    pub persons: Vec<Actor>,
    /// The whole terrain, one string per row of world cells: '.' outdoor ground, ',' indoor
    /// floor, '+' doorway, '#' wall, '%' fence / hedge, '~' water, ' ' wilderness.
    #[serde(default)]
    pub tiles: Vec<String>,
}

impl WorldView {
    /// The place a world cell belongs to (walls, fences and wilderness belong to none).
    pub fn region_at(&self, cell: IVec2) -> Option<&RegionView> {
        self.regions.iter().find(|r| r.contains(cell))
    }
}

#[derive(Deserialize, Clone, Default)]
pub struct RegionView {
    #[serde(default, deserialize_with = "lenient_string")]
    pub name: String,
    #[serde(default)]
    pub x: i32,
    #[serde(default)]
    pub y: i32,
    #[serde(default)]
    pub w: i32,
    #[serde(default)]
    pub h: i32,
    /// This place's own ground inside the bounding area: one string per row, '#' = ground
    /// (empty = the whole rectangle). Walls and buildings nested inside are not part of it.
    #[serde(default)]
    pub rows: Vec<String>,
    #[serde(default, deserialize_with = "lenient_string")]
    pub description: String,
    /// A room inside a building (walls around it, entered through a doorway).
    #[serde(default)]
    pub indoor: bool,
    /// The outdoor place an indoor room's building stands in.
    #[serde(default, deserialize_with = "lenient_string")]
    pub parent: String,
    /// A cell in the middle of this place's ground (label / where to walk to).
    #[serde(default)]
    pub cx: Option<i32>,
    #[serde(default)]
    pub cy: Option<i32>,
}

impl RegionView {
    /// On this region's ground (inside its shape, not just its bounding rectangle).
    pub fn contains(&self, cell: IVec2) -> bool {
        let inside = cell.x >= self.x && cell.x < self.x + self.w && cell.y >= self.y && cell.y < self.y + self.h;
        if !inside || self.rows.is_empty() {
            return inside;
        }
        self.rows
            .get((cell.y - self.y) as usize)
            .and_then(|row| row.as_bytes().get((cell.x - self.x) as usize))
            .is_some_and(|c| *c == b'#')
    }

    pub fn center(&self) -> IVec2 {
        match (self.cx, self.cy) {
            (Some(x), Some(y)) => IVec2::new(x, y),
            _ => IVec2::new(self.x + self.w / 2, self.y + self.h / 2),
        }
    }
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
    #[serde(default, deserialize_with = "lenient_i32")]
    pub coins: i32,
    /// Buys and sells things (Trade in their menu).
    #[serde(default)]
    pub trader: bool,
    /// Travelling with this person (party leader), "" = on their own.
    #[serde(default, deserialize_with = "lenient_string")]
    pub follows: String,
    #[serde(default, deserialize_with = "lenient_vec")]
    pub allies: Vec<String>,
    /// World cell (seamless world).
    #[serde(default)]
    pub x: Option<i32>,
    #[serde(default)]
    pub y: Option<i32>,
    /// Why this person treats the player as an enemy ("" = they don't).
    #[serde(default, deserialize_with = "lenient_string")]
    pub hostile: String,
}

impl Actor {
    pub fn cell(&self) -> Option<IVec2> {
        Some(IVec2::new(self.x?, self.y?))
    }
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
    /// Fixed actions this object offers (server tells whether each is doable right now).
    #[serde(default)]
    pub actions: Vec<ThingAction>,
    /// A construction site's missing materials ("3 log, 2 stone"; "" = none / not a site).
    #[serde(default, deserialize_with = "lenient_string")]
    pub needs: String,
    /// A gathering spot's remaining supply (it regrows in fractions; shown whole).
    #[serde(default, deserialize_with = "lenient_opt_i32")]
    pub stock: Option<i32>,
    /// Whose it is ("" = nobody's: finders keepers). Taking someone else's is stealing.
    #[serde(default, deserialize_with = "lenient_string")]
    pub owner: String,
}

#[derive(Deserialize, Clone, Default)]
pub struct ThingAction {
    #[serde(default, deserialize_with = "lenient_string")]
    pub id: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub label: String,
    #[serde(default)]
    pub available: bool,
    #[serde(default, deserialize_with = "lenient_string")]
    pub reason: String,
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
    /// Server simulation clock, in sim seconds.
    #[serde(default)]
    pub sim_time: f32,
    #[serde(default)]
    pub running: bool,
    #[serde(default)]
    pub interval_seconds: f32,
    #[serde(default)]
    pub sim_dt: f32,
    #[serde(default)]
    pub clock: Clock,
    #[serde(default)]
    pub npc_activity: Vec<NpcActivity>,
    /// World announcements, newest first.
    #[serde(default)]
    pub chronicle: Vec<ChronicleEntry>,
    /// Recent conversations, oldest first (played back as speech bubbles).
    #[serde(default)]
    pub conversations: Vec<Conversation>,
    /// Recent mutters (NPCs talking to themselves on the road), oldest first.
    #[serde(default)]
    pub mutters: Vec<Mutter>,
}

#[derive(Deserialize, Clone, Default)]
pub struct Mutter {
    #[serde(default, deserialize_with = "lenient_string")]
    pub id: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub who: String,
    /// Empty: just the mood icon.
    #[serde(default, deserialize_with = "lenient_string")]
    pub text: String,
    /// Mood icon key (happy, angry, sad, scared, tired, love, surprised, thinking, hurt, secret).
    #[serde(default, deserialize_with = "lenient_string")]
    pub icon: String,
    /// Something that matters (a secret slipping out); `overheard` when the player was close enough.
    #[serde(default)]
    pub important: bool,
    #[serde(default)]
    pub overheard: bool,
}

#[derive(Deserialize, Clone, Default)]
pub struct Conversation {
    #[serde(default, deserialize_with = "lenient_string")]
    pub id: String,
    /// The two people talking (both stand still until it has been said).
    #[serde(default, deserialize_with = "lenient_string")]
    pub a: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub b: String,
    #[serde(default)]
    pub lines: Vec<ConversationLine>,
}

#[derive(Deserialize, Clone, Default)]
pub struct ConversationLine {
    #[serde(default, deserialize_with = "lenient_string")]
    pub speaker: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub text: String,
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
    /// Sim time the pending action will complete on the server.
    #[serde(default)]
    pub pending_completes_at: Option<f32>,
    /// World cells the NPC walks before the action happens (start included).
    #[serde(default)]
    pub pending_path: Option<Vec<[i32; 2]>>,
    /// Sim time the pending action started, and how much of it is walking (sim seconds).
    #[serde(default)]
    pub pending_started_at: Option<f32>,
    #[serde(default)]
    pub pending_walk_seconds: Option<f32>,
    /// Plan progress: step `plan_step` of `plan_len`, that step in words, and what they are doing now.
    #[serde(default)]
    pub plan_step: usize,
    #[serde(default)]
    pub plan_len: usize,
    #[serde(default, deserialize_with = "lenient_string")]
    pub step_line: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub now_label: String,
    /// Their open request ("needs stone (3c)"), shown as a "!" over their head.
    #[serde(default, deserialize_with = "lenient_string")]
    pub request: String,
    /// A background NPC thinking with the LLM for a while (emergency / stuck).
    #[serde(default)]
    pub thinking_hard: bool,
    #[serde(default, deserialize_with = "lenient_string")]
    pub goal: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub target: String,
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

fn lenient_opt_i32<'de, D: serde::Deserializer<'de>>(de: D) -> Result<Option<i32>, D::Error> {
    Ok(Value::deserialize(de)?.as_f64().map(|v| v.floor() as i32))
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
    let ServerAddr {
        host,
        port,
        host_port,
        full_path,
    } = ServerAddr::parse(base_url, path)?;
    let (host, host_port) = (host.as_str(), host_port.as_str());

    let mut stream = TcpStream::connect((host, port))
        .map_err(|err| format!("Could not connect to {host}:{port}: {err}"))?;
    // Writing a questline is several LLM rounds plus planning: give it time.
    let read_timeout = if path.contains("/questlines/") { 900 } else if method == "POST" { 90 } else { 15 };
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

struct ServerAddr {
    host: String,
    port: u16,
    host_port: String,
    full_path: String,
}

impl ServerAddr {
    fn parse(base_url: &str, path: &str) -> Result<Self, String> {
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
        Ok(Self {
            host: host.to_string(),
            port,
            host_port: host_port.to_string(),
            full_path,
        })
    }
}

// ---------------------------------------------------------------------------
// Server push: `/api/stream` (Server-Sent Events)
// ---------------------------------------------------------------------------

/// Keeps one SSE connection open on its own thread, reconnecting with backoff.
/// Every `event: state` frame becomes a normal `NetResponse::State`, so the rest of
/// the client handles pushed and polled state the same way.
fn spawn_state_stream(server: String, tx: Sender<NetResponse>, streaming: Arc<AtomicBool>) {
    thread::spawn(move || {
        let mut backoff = Duration::from_secs(1);
        loop {
            let started = Instant::now();
            let receiver_gone = read_state_stream(&server, &tx, &streaming).is_err();
            streaming.store(false, Ordering::Relaxed);
            if receiver_gone {
                break;
            }
            if started.elapsed() > Duration::from_secs(10) {
                backoff = Duration::from_secs(1);
            }
            thread::sleep(backoff);
            backoff = (backoff * 2).min(STREAM_MAX_BACKOFF);
        }
    });
}

/// Returns `Err(())` only when the app side hung up; connection problems return `Ok(())`
/// so the caller reconnects.
fn read_state_stream(
    server: &str,
    tx: &Sender<NetResponse>,
    streaming: &AtomicBool,
) -> Result<(), ()> {
    let Ok(addr) = ServerAddr::parse(server, "/api/stream") else {
        return Ok(());
    };
    let Ok(mut stream) = TcpStream::connect((addr.host.as_str(), addr.port)) else {
        return Ok(());
    };
    let _ = stream.set_read_timeout(Some(STREAM_READ_TIMEOUT));
    let request = format!(
        "GET {} HTTP/1.1\r\nHost: {}\r\nAccept: text/event-stream\r\nCache-Control: no-cache\r\n\r\n",
        addr.full_path, addr.host_port
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return Ok(());
    }

    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    if reader.read_line(&mut line).is_err() || !line.contains(" 200") {
        return Ok(());
    }
    let mut chunked = false;
    loop {
        line.clear();
        if reader.read_line(&mut line).unwrap_or(0) == 0 {
            return Ok(());
        }
        let header = line.trim();
        if header.is_empty() {
            break;
        }
        if header.eq_ignore_ascii_case("transfer-encoding: chunked") {
            chunked = true;
        }
    }
    streaming.store(true, Ordering::Relaxed);

    let mut events = SseBuffer::default();
    loop {
        let Some(bytes) = next_body_bytes(&mut reader, chunked) else {
            return Ok(());
        };
        for data in events.push(&bytes) {
            // A frame we can't read is reported (shown as the offline reason), not dropped:
            // polling is paused while the stream is up, so silence would look like "offline".
            let response = match serde_json::from_str::<PlayState>(&data) {
                Ok(state) => NetResponse::State(Ok(state)),
                Err(err) => NetResponse::State(Err(format!("Bad state from server: {err}"))),
            };
            if tx.send(response).is_err() {
                return Err(());
            }
        }
    }
}

/// Next piece of the response body, de-chunked. `None` = connection over.
fn next_body_bytes(reader: &mut BufReader<TcpStream>, chunked: bool) -> Option<Vec<u8>> {
    if !chunked {
        let buf = reader.fill_buf().ok()?;
        if buf.is_empty() {
            return None;
        }
        let out = buf.to_vec();
        reader.consume(out.len());
        return Some(out);
    }
    let mut size_line = String::new();
    if reader.read_line(&mut size_line).ok()? == 0 {
        return None;
    }
    let size_hex = size_line.trim().split(';').next().unwrap_or("");
    let size = usize::from_str_radix(size_hex, 16).ok()?;
    if size == 0 {
        return None;
    }
    let mut out = vec![0u8; size];
    reader.read_exact(&mut out).ok()?;
    let mut crlf = [0u8; 2];
    reader.read_exact(&mut crlf).ok()?;
    Some(out)
}

/// Splits an SSE byte stream into the `data` payloads of `event: state` frames.
#[derive(Default)]
struct SseBuffer {
    pending: Vec<u8>,
}

impl SseBuffer {
    fn push(&mut self, bytes: &[u8]) -> Vec<String> {
        self.pending.extend(bytes.iter().filter(|&&b| b != b'\r'));
        let mut out = Vec::new();
        while let Some(end) = self.pending.windows(2).position(|w| w == b"\n\n") {
            let frame: Vec<u8> = self.pending.drain(..end + 2).collect();
            let frame = String::from_utf8_lossy(&frame);
            let mut event = "message";
            let mut data: Vec<&str> = Vec::new();
            for line in frame.lines() {
                if let Some(v) = line.strip_prefix("event:") {
                    event = v.trim();
                } else if let Some(v) = line.strip_prefix("data:") {
                    data.push(v.strip_prefix(' ').unwrap_or(v));
                }
            }
            if event == "state" && !data.is_empty() {
                out.push(data.join("\n"));
            }
        }
        out
    }
}

// ---------------------------------------------------------------------------
// Simulation clock (maps server sim seconds to local wall time)
// ---------------------------------------------------------------------------

/// Tracks the server's sim clock between state updates so animations can be timed
/// to finish when the server-side action does.
#[derive(Resource)]
pub struct SimClock {
    sim_time: f32,
    at: Instant,
    running: bool,
    /// Real seconds per sim second, smoothed over recent updates.
    wall_per_sim: f32,
}

impl Default for SimClock {
    fn default() -> Self {
        Self {
            sim_time: 0.0,
            at: Instant::now(),
            running: false,
            wall_per_sim: 1.0,
        }
    }
}

impl SimClock {
    pub fn observe(&mut self, sim: &Simulation) {
        let now = Instant::now();
        let advanced = sim.sim_time - self.sim_time;
        if self.running && sim.running && advanced > 0.0 {
            let measured = now.duration_since(self.at).as_secs_f32() / advanced;
            self.wall_per_sim = (0.7 * self.wall_per_sim + 0.3 * measured).clamp(0.02, 20.0);
        } else if sim.running && !self.running && sim.sim_dt > 0.0 && sim.interval_seconds > 0.0 {
            // Fresh start: the configured pace is the best first guess.
            self.wall_per_sim = sim.interval_seconds / sim.sim_dt;
        }
        if advanced != 0.0 || sim.running != self.running {
            self.sim_time = sim.sim_time;
            self.at = now;
        }
        self.running = sim.running;
    }

    /// Estimated current server sim time (extrapolated while running).
    pub fn now(&self) -> f32 {
        if !self.running {
            return self.sim_time;
        }
        let elapsed = self.at.elapsed().as_secs_f32() / self.wall_per_sim.max(1e-3);
        self.sim_time + elapsed
    }

}

// ---------------------------------------------------------------------------
// Player position reports (seamless world)
// ---------------------------------------------------------------------------

/// One thread, latest position wins: queued reports collapse to the newest cell.
fn spawn_position_reporter(server: String) -> Sender<IVec2> {
    let (tx, rx) = mpsc::channel::<IVec2>();
    thread::spawn(move || {
        while let Ok(mut cell) = rx.recv() {
            while let Ok(newer) = rx.try_recv() {
                cell = newer;
            }
            let body = json!({ "x": cell.x, "y": cell.y }).to_string();
            let _ = http_json(&server, "POST", "/api/player/position", Some(body));
        }
    });
    tx
}

impl SimClock {
    /// Real seconds one sim second takes right now (1.0 until measured).
    pub fn wall_per_sim(&self) -> f32 {
        self.wall_per_sim
    }
}

#[cfg(test)]
mod tests {
    use super::SseBuffer;

    #[test]
    fn sse_frames_split_across_reads() {
        let mut buf = SseBuffer::default();
        assert!(buf.push(b": connected\n\nevent: state\ndata: {\"a\"").is_empty());
        let out = buf.push(b":1}\n\n: keepalive\n\nevent: state\r\ndata: 2\r\n\r\n");
        assert_eq!(out, vec!["{\"a\":1}".to_string(), "2".to_string()]);
    }

    #[test]
    fn other_events_are_ignored() {
        let mut buf = SseBuffer::default();
        assert!(buf.push(b"event: ping\ndata: x\n\n").is_empty());
    }
}


/// Director tab: the storyteller's arc and recent moves, conflicts, grudges, questlines.
#[derive(Deserialize, Clone, Default)]
pub struct DirectorData {
    #[serde(default, deserialize_with = "lenient_string")]
    pub mode: String,
    #[serde(default)]
    pub thinking: bool,
    #[serde(default, deserialize_with = "lenient_string")]
    pub arc: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub next_clock: String,
    #[serde(default)]
    pub next_in_hours: Option<f32>,
    #[serde(default)]
    pub beats: Vec<DirectorLine>,
    #[serde(default)]
    pub conflicts: Vec<DirectorLine>,
    #[serde(default)]
    pub tensions: Vec<DirectorLine>,
    #[serde(default)]
    pub chronicle: Vec<DirectorLine>,
    #[serde(default)]
    pub questlines: Vec<QuestlineView>,
    /// Prices by kind of thing (supply and demand).
    #[serde(default)]
    pub market: Vec<MarketRow>,
    #[serde(default)]
    pub alliances: Vec<AllianceView>,
    #[serde(default)]
    pub requests: Vec<DirectorLine>,
}

#[derive(Deserialize, Clone, Default)]
pub struct MarketRow {
    #[serde(default, deserialize_with = "lenient_string")]
    pub kind: String,
    #[serde(default)]
    pub price: i64,
    #[serde(default)]
    pub factor: f32,
    #[serde(default)]
    pub supply: i64,
    #[serde(default)]
    pub demand: f32,
    /// "up" | "down" | ""
    #[serde(default, deserialize_with = "lenient_string")]
    pub trend: String,
}

#[derive(Deserialize, Clone, Default)]
pub struct AllianceView {
    #[serde(default, deserialize_with = "lenient_string")]
    pub name: String,
    #[serde(default, deserialize_with = "lenient_vec")]
    pub members: Vec<String>,
    #[serde(default)]
    pub stances: Vec<StanceView>,
}

#[derive(Deserialize, Clone, Default)]
pub struct StanceView {
    #[serde(default, deserialize_with = "lenient_string")]
    pub other: String,
    #[serde(default)]
    pub value: i64,
    #[serde(default, deserialize_with = "lenient_string")]
    pub word: String,
}

#[derive(Deserialize, Clone, Default)]
pub struct DirectorLine {
    #[serde(default, deserialize_with = "lenient_string")]
    pub clock: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub line: String,
}

#[derive(Deserialize, Clone, Default)]
pub struct QuestlineView {
    #[serde(default, deserialize_with = "lenient_string")]
    pub title: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub premise: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub state: String,
    #[serde(default)]
    pub stages: Vec<QuestStageView>,
}

#[derive(Deserialize, Clone, Default)]
pub struct QuestStageView {
    #[serde(default, deserialize_with = "lenient_string")]
    pub title: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub objective: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub giver: String,
    /// "done" | "current" | "todo"
    #[serde(default, deserialize_with = "lenient_string")]
    pub state: String,
}

/// A questline the LLM just wrote (not in the world until applied).
#[derive(Deserialize, Clone, Default)]
pub struct DraftSummary {
    #[serde(default)]
    pub ok: bool,
    #[serde(default)]
    pub lines: Vec<String>,
    #[serde(default, deserialize_with = "lenient_string")]
    pub title: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub premise: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub hook: String,
    /// New places / people / items / clues / stages, each with its details.
    #[serde(default)]
    pub sections: Vec<DraftSection>,
    #[serde(default)]
    pub problems: Vec<String>,
    #[serde(default)]
    pub attempts: Option<u32>,
}

#[derive(Deserialize, Clone, Default)]
pub struct DraftSection {
    #[serde(default, deserialize_with = "lenient_string")]
    pub title: String,
    #[serde(default)]
    pub lines: Vec<String>,
}

/// One save file (what the Save / Load menu shows).
#[derive(Deserialize, Clone, Default)]
pub struct SaveInfo {
    #[serde(default, deserialize_with = "lenient_string")]
    pub slot: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub saved_at: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub clock: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub player: String,
}

/// Percent-encode a query-string value (UTF-8, so Chinese searches work too).
pub fn url_encode(text: &str) -> String {
    let mut out = String::new();
    for b in text.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// What an NPC remembers and knows (NPC tab: Memories / Knows pages).
#[derive(Deserialize, Clone, Default)]
pub struct MindData {
    #[serde(default)]
    pub available: bool,
    #[serde(default)]
    pub total: usize,
    #[serde(default)]
    pub types: std::collections::BTreeMap<String, usize>,
    #[serde(default)]
    pub memories: Vec<MemoryView>,
    #[serde(default)]
    pub knows: KnowsView,
    #[serde(default)]
    pub profile: ProfileView,
    #[serde(default)]
    pub routine: RoutineView,
}

/// The character as written (persona fields) and how they are right now.
#[derive(Deserialize, Clone, Default)]
pub struct ProfileView {
    #[serde(default)]
    pub fields: Vec<ProfileField>,
    #[serde(default)]
    pub status: Vec<String>,
    /// "llm" (decides with the language model) | "routine" (background NPC) | "" (the player)
    #[serde(default, deserialize_with = "lenient_string")]
    pub brain: String,
}

#[derive(Deserialize, Clone, Default)]
pub struct ProfileField {
    #[serde(default, deserialize_with = "lenient_string")]
    pub label: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub value: String,
}

/// Their day: each slot, which one is now, which once-a-day ones are done.
#[derive(Deserialize, Clone, Default)]
pub struct RoutineView {
    #[serde(default)]
    pub slots: Vec<RoutineSlotView>,
    /// A held state such as "Asleep" (empty when none).
    #[serde(default, deserialize_with = "lenient_string")]
    pub state: String,
}

#[derive(Deserialize, Clone, Default)]
pub struct RoutineSlotView {
    #[serde(default, deserialize_with = "lenient_string")]
    pub hours: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub r#where: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub what: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub notes: String,
    #[serde(default)]
    pub now: bool,
    #[serde(default)]
    pub done: bool,
}

#[derive(Deserialize, Clone, Default)]
pub struct MemoryView {
    #[serde(default, deserialize_with = "lenient_string")]
    pub text: String,
    #[serde(default, rename = "type", deserialize_with = "lenient_string")]
    pub kind: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub location: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub time: String,
}

#[derive(Deserialize, Clone, Default)]
pub struct KnowsView {
    /// How they feel about people ("Likes Sage (4) [ally] - gave me berries").
    #[serde(default)]
    pub relations: Vec<String>,
    #[serde(default)]
    pub facts: Vec<FactView>,
    #[serde(default)]
    pub places: Vec<String>,
    #[serde(default)]
    pub seen: Vec<String>,
    #[serde(default)]
    pub omniscient: bool,
    #[serde(default)]
    pub grudges: Vec<String>,
    #[serde(default)]
    pub trust: Option<i32>,
    #[serde(default, deserialize_with = "lenient_string")]
    pub player: String,
}

#[derive(Deserialize, Clone, Default)]
pub struct FactView {
    #[serde(default, deserialize_with = "lenient_string")]
    pub kind: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub text: String,
}

#[cfg(test)]
mod url_tests {
    use super::url_encode;

    #[test]
    fn a_regrowing_stock_with_decimals_still_parses() {
        let thing: super::Thing = serde_json::from_str(r#"{"name": "QuarryFace", "stock": 5.0}"#).unwrap();
        assert_eq!(thing.stock, Some(5));
        let thing: super::Thing = serde_json::from_str(r#"{"name": "Pier", "stock": 3.7}"#).unwrap();
        assert_eq!(thing.stock, Some(3), "shown whole: 3.7 left is 3 you can take");
    }

    #[test]
    fn query_values_are_percent_encoded() {
        assert_eq!(url_encode("oak tree"), "oak%20tree");
        assert_eq!(url_encode("木头"), "%E6%9C%A8%E5%A4%B4");
        assert_eq!(url_encode("a&b=c"), "a%26b%3Dc");
    }
}

/// One recipe as the Craft panel shows it.
#[derive(Deserialize, Clone, Default)]
pub struct RecipeView {
    #[serde(default, deserialize_with = "lenient_string")]
    pub action: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub label: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub description: String,
    #[serde(default)]
    pub inputs: Vec<RecipeInput>,
    #[serde(default, deserialize_with = "lenient_string")]
    pub station: String,
    #[serde(default)]
    pub at_station: bool,
    #[serde(default)]
    pub ready: bool,
}

#[derive(Deserialize, Clone, Default)]
pub struct RecipeInput {
    #[serde(default, deserialize_with = "lenient_string")]
    pub kind: String,
    #[serde(default)]
    pub need: u32,
    #[serde(default)]
    pub have: u32,
}

/// A trader's offer: what they sell, what they would buy from you.
#[derive(Deserialize, Clone, Default)]
pub struct TradeView {
    #[serde(default, deserialize_with = "lenient_string")]
    pub npc: String,
    #[serde(default)]
    pub trader: bool,
    #[serde(default)]
    pub my_coins: i64,
    #[serde(default)]
    pub npc_coins: i64,
    #[serde(default)]
    pub sells: Vec<TradeGood>,
    #[serde(default)]
    pub buys: Vec<TradeGood>,
}

#[derive(Deserialize, Clone, Default)]
pub struct TradeGood {
    #[serde(default, deserialize_with = "lenient_string")]
    pub name: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub label: String,
    #[serde(default)]
    pub price: i64,
    #[serde(default)]
    pub affordable: bool,
}

/// A request someone posted ("needs stone - pays 3 coins").
#[derive(Deserialize, Clone, Default)]
pub struct RequestView {
    #[serde(default, deserialize_with = "lenient_string")]
    pub id: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub poster: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub kind: String,
    /// "open" | "done" | "cancelled"
    #[serde(default, deserialize_with = "lenient_string")]
    pub status: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub reason: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub taker: String,
    #[serde(default, deserialize_with = "lenient_string")]
    pub note: String,
    /// Your items of that kind (you could deliver).
    #[serde(default, deserialize_with = "lenient_vec")]
    pub you_have: Vec<String>,
    #[serde(default, deserialize_with = "lenient_string")]
    pub line: String,
}
