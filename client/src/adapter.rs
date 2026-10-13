//! Command → behaviour adapter.
//!
//! The server simulation decides *what* NPCs do (`move(Lila, Home, Clinic)`,
//! `pick_up(Rowan, Herb, Clinic)`, ...) and applies the effects to the graph.
//! This module turns those commands, plus the location changes we observe
//! between syncs, into visible behaviour: walk to the exit and use it, walk up
//! to an item and pick it up, walk over to someone and talk, and so on.

use bevy::prelude::*;
use std::collections::{HashMap, VecDeque};

use crate::net::{NpcActivity, WorldSnapshot};
use crate::scene::{
    clamp_to_room, exit_position_for, npc_home, Interactive, PlayerAvatar, TargetKind,
};

const NPC_SPEED: f32 = 170.0;
const ARRIVE_EPS: f32 = 3.0;
const APPROACH_GAP: f32 = 42.0;

#[derive(Clone, Debug)]
enum Step {
    WalkTo(Vec2),
    /// Stand still showing `text` above the head for `secs` seconds.
    Act { text: String, secs: f32 },
    Despawn,
}

#[derive(Component)]
pub struct NpcAvatar {
    name: String,
    home: Vec2,
    queue: VecDeque<Step>,
    current: Option<Step>,
    /// Walking out through an exit; never returns home.
    leaving: bool,
    /// Already gone from this room according to the server.
    gone: bool,
    pending_key: String,
    bubble: String,
}

#[derive(Component)]
pub struct NpcBubble {
    owner: Entity,
}

#[derive(Resource, Default)]
pub struct NpcDirector {
    room: String,
    revision: Option<u64>,
    known_locations: HashMap<String, String>,
}

pub fn sync_npcs(
    mut commands: Commands,
    world: Res<WorldSnapshot>,
    mut director: ResMut<NpcDirector>,
    mut avatars: Query<(Entity, &mut NpcAvatar, &Transform)>,
    targets: Query<(&Transform, &Interactive), Without<NpcAvatar>>,
    player: Query<&Transform, (With<PlayerAvatar>, Without<NpcAvatar>)>,
) {
    if director.revision == Some(world.revision) {
        return;
    }
    director.revision = Some(world.revision);

    let room = world.current_location.name.clone();
    let room_changed = director.room != room;
    if room_changed {
        for (entity, _, _) in &avatars {
            commands.entity(entity).despawn();
        }
        director.room = room.clone();
    }

    // Where things are right now, for "walk over to X".
    let mut positions: HashMap<String, Vec2> = HashMap::new();
    for (transform, interactive) in &targets {
        positions.insert(interactive.name.clone(), transform.translation.truncate());
    }
    for (_, avatar, transform) in &avatars {
        positions.insert(avatar.name.clone(), transform.translation.truncate());
    }
    if let Ok(transform) = player.single() {
        positions.insert(world.player.name.clone(), transform.translation.truncate());
    }

    let mut here = world.people_here.clone();
    here.sort_by(|a, b| a.name.cmp(&b.name));
    let mut seen: Vec<String> = Vec::new();

    for (idx, actor) in here.iter().enumerate() {
        let home = npc_home(&world, &actor.name, idx);
        seen.push(actor.name.clone());

        let existing = if room_changed {
            None
        } else {
            avatars
                .iter_mut()
                .find(|(_, avatar, _)| avatar.name == actor.name && !avatar.gone)
        };

        if let Some((_, mut avatar, _)) = existing {
            avatar.home = home;
            continue;
        }

        // New arrival: enter through the exit they came from, if we know it.
        let came_from = director.known_locations.get(&actor.name).cloned();
        let entry = came_from
            .filter(|prev| !room_changed && *prev != room)
            .and_then(|prev| exit_position_for(&world, &prev));
        let mut queue = VecDeque::new();
        let start = match entry {
            Some(door) => {
                queue.push_back(Step::WalkTo(home));
                door
            }
            None => home,
        };
        spawn_npc(&mut commands, &actor.name, actor.alive, actor.hp, start, home, queue);
    }

    // Left the room (or died / vanished): walk out through the matching exit.
    if !room_changed {
        for (entity, mut avatar, _) in &mut avatars {
            if avatar.gone || seen.contains(&avatar.name) {
                continue;
            }
            avatar.gone = true;
            commands.entity(entity).remove::<Interactive>();
            if avatar.leaving {
                avatar.queue.push_back(Step::Despawn);
                continue;
            }
            let destination = world
                .character(&avatar.name)
                .and_then(|c| c.location.clone())
                .unwrap_or_default();
            avatar.queue.clear();
            avatar.current = None;
            avatar.leaving = true;
            if let Some(door) = exit_position_for(&world, &destination) {
                avatar.queue.push_back(Step::WalkTo(door));
                avatar.queue.push_back(Step::Act {
                    text: format!("-> {destination}"),
                    secs: 0.4,
                });
            }
            avatar.queue.push_back(Step::Despawn);
        }

        // Translate what each NPC is currently doing into steps.
        for (_, mut avatar, transform) in &mut avatars {
            if avatar.gone {
                continue;
            }
            let Some(activity) = world.activity(&avatar.name) else {
                continue;
            };
            if activity.pending_action.is_empty() {
                avatar.pending_key.clear();
                // A move that never happened (rejected / replanned): come back inside.
                avatar.leaving = false;
                continue;
            }
            let key = format!("{}({})", activity.pending_action, activity.pending_args.join(","));
            if key == avatar.pending_key {
                continue;
            }
            avatar.pending_key = key;
            let me = transform.translation.truncate();
            let (steps, leaving) = command_to_steps(activity, &world, &positions, me);
            if steps.is_empty() {
                continue;
            }
            avatar.queue = steps.into();
            avatar.current = None;
            avatar.leaving = leaving;
        }
    }

    director.known_locations = world
        .characters
        .iter()
        .filter_map(|c| c.location.clone().map(|loc| (c.name.clone(), loc)))
        .collect();
}

/// The adapter table: one simulation command → a short behaviour script.
fn command_to_steps(
    activity: &NpcActivity,
    world: &WorldSnapshot,
    positions: &HashMap<String, Vec2>,
    me: Vec2,
) -> (Vec<Step>, bool) {
    let args = &activity.pending_args;
    let arg = |i: usize| args.get(i).cloned().unwrap_or_default();
    let approach = |name: &str| {
        positions.get(name).map(|&target| {
            let away = (me - target).normalize_or(Vec2::NEG_Y);
            clamp_to_room(target + away * APPROACH_GAP)
        })
    };
    let walk_then = |target: &str, text: String, secs: f32| {
        let mut steps = Vec::new();
        if let Some(spot) = approach(target) {
            steps.push(Step::WalkTo(spot));
        }
        steps.push(Step::Act { text, secs });
        steps
    };

    match activity.pending_action.as_str() {
        "move" => {
            let to = arg(2);
            match exit_position_for(world, &to) {
                Some(door) => (
                    vec![
                        Step::WalkTo(door),
                        Step::Act {
                            text: format!("-> {to}"),
                            secs: 0.6,
                        },
                    ],
                    true,
                ),
                None => (Vec::new(), false),
            }
        }
        "talk" => {
            let other = arg(1);
            (walk_then(&other, format!("talking to {other}..."), 3.5), false)
        }
        "pick_up" => {
            let item = arg(1);
            (walk_then(&item, format!("picks up {item}"), 1.0), false)
        }
        "give" => {
            let (item, receiver) = (arg(1), arg(2));
            (walk_then(&receiver, format!("gives {item} to {receiver}"), 1.2), false)
        }
        "attack" => {
            let target = arg(1);
            (walk_then(&target, format!("attacks {target}!"), 1.2), false)
        }
        "use_on" => {
            let (item, target) = (arg(1), arg(2));
            (walk_then(&target, format!("uses {item} on {target}"), 1.5), false)
        }
        "interact" => {
            let target = arg(1);
            (walk_then(&target, format!("uses {target}"), 2.5), false)
        }
        "drop" => (
            vec![Step::Act {
                text: format!("drops {}", arg(1)),
                secs: 1.0,
            }],
            false,
        ),
        "use_item" => (
            vec![Step::Act {
                text: format!("uses {}", arg(1)),
                secs: 1.0,
            }],
            false,
        ),
        "wait" | "noop" | "" => (Vec::new(), false),
        other => (
            vec![Step::Act {
                text: other.to_string(),
                secs: 1.0,
            }],
            false,
        ),
    }
}

fn spawn_npc(
    commands: &mut Commands,
    name: &str,
    alive: bool,
    hp: i32,
    start: Vec2,
    home: Vec2,
    queue: VecDeque<Step>,
) {
    let color = if alive {
        Color::srgb(0.78, 0.32, 0.43)
    } else {
        Color::srgb(0.28, 0.29, 0.31)
    };
    let npc = commands
        .spawn((
            Sprite {
                color,
                custom_size: Some(Vec2::splat(46.0)),
                ..default()
            },
            Transform::from_xyz(start.x, start.y, 5.0),
            NpcAvatar {
                name: name.to_string(),
                home,
                queue,
                current: None,
                leaving: false,
                gone: false,
                pending_key: String::new(),
                bubble: String::new(),
            },
            Interactive {
                kind: TargetKind::Person {
                    name: name.to_string(),
                },
                prompt: format!("{name}  (HP {hp})"),
                name: name.to_string(),
                radius: 56.0,
            },
        ))
        .id();
    commands.entity(npc).with_children(|parent| {
        parent.spawn((
            Text2d::new(name),
            TextFont {
                font_size: 12.0,
                ..default()
            },
            TextColor(Color::srgb(0.98, 0.88, 0.84)),
            Transform::from_xyz(0.0, -36.0, 0.2),
        ));
        parent.spawn((
            Text2d::new(""),
            TextFont {
                font_size: 13.0,
                ..default()
            },
            TextColor(Color::srgb(1.0, 0.92, 0.55)),
            Transform::from_xyz(0.0, 40.0, 0.3),
            NpcBubble { owner: npc },
        ));
    });
}

pub fn run_npc_behaviour(
    mut commands: Commands,
    time: Res<Time>,
    mut avatars: Query<(Entity, &mut NpcAvatar, &mut Transform)>,
) {
    let dt = time.delta_secs();
    for (entity, mut avatar, mut transform) in &mut avatars {
        if avatar.current.is_none() {
            avatar.current = avatar.queue.pop_front();
        }
        if avatar.current.is_none() && !avatar.leaving && !avatar.gone {
            let pos = transform.translation.truncate();
            if pos.distance(avatar.home) > ARRIVE_EPS {
                avatar.current = Some(Step::WalkTo(avatar.home));
            }
        }

        let mut finished = false;
        let mut bubble = String::new();
        match avatar.current.as_mut() {
            Some(Step::WalkTo(dest)) => {
                let pos = transform.translation.truncate();
                let delta = *dest - pos;
                let step = NPC_SPEED * dt;
                if delta.length() <= step.max(ARRIVE_EPS) {
                    transform.translation.x = dest.x;
                    transform.translation.y = dest.y;
                    finished = true;
                } else {
                    let next = pos + delta.normalize() * step;
                    transform.translation.x = next.x;
                    transform.translation.y = next.y;
                }
            }
            Some(Step::Act { text, secs }) => {
                bubble = text.clone();
                *secs -= dt;
                finished = *secs <= 0.0;
            }
            Some(Step::Despawn) => {
                commands.entity(entity).despawn();
                continue;
            }
            None => {}
        }
        if finished {
            avatar.current = None;
        }
        if avatar.bubble != bubble {
            avatar.bubble = bubble;
        }
    }
}

pub fn update_npc_bubbles(
    world: Res<WorldSnapshot>,
    avatars: Query<&NpcAvatar>,
    mut bubbles: Query<(&NpcBubble, &mut Text2d)>,
) {
    for (bubble, mut text) in &mut bubbles {
        let Ok(avatar) = avatars.get(bubble.owner) else {
            continue;
        };
        let state_label = world
            .activity(&avatar.name)
            .map(|a| a.state_label.clone())
            .unwrap_or_default();
        let next = if !state_label.is_empty() && avatar.bubble.is_empty() {
            state_label
        } else {
            avatar.bubble.clone()
        };
        if text.0 != next {
            text.0 = next;
        }
    }
}
