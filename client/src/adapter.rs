//! NPCs in the seamless world.
//!
//! The server decides what each NPC does and, for anything that needs walking, sends the
//! route (`pending_path`, world cells) and when the action completes. Here every NPC lives
//! in the one world scene and follows that route so it arrives when the server's walk
//! ends, then shows what it is doing above its head. Without a route it glides to the
//! cell the server says it stands on.

use bevy::prelude::*;
use std::time::Instant;

use crate::net::{trim_line, NpcActivity, SimClock, WorldSnapshot};
use crate::speech::SpeechPlayer;
use crate::scene::{cell_center, Interactive, TargetKind, TILE};

/// Server walking speed (world cells per sim second); must match spatial.WALK_CELLS_PER_SEC.
const SERVER_WALK_SPEED: f32 = 10.0;
/// Fallback glide speed when correcting to the server position (pixels per second).
const GLIDE_SPEED: f32 = 6.0 * TILE;
const MIN_WALK_SECS: f32 = 0.2;

#[derive(Component)]
pub struct NpcAvatar {
    name: String,
    /// Route + timing currently being walked.
    walk: Option<Walk>,
    /// Identifies the server action the current walk belongs to.
    walk_key: String,
    bubble: String,
}

impl NpcAvatar {
    pub fn name(&self) -> &str {
        &self.name
    }
}

struct Walk {
    points: Vec<Vec2>,
    started: Instant,
    secs: f32,
}

impl Walk {
    fn position(&self) -> (Vec2, bool) {
        let t = (self.started.elapsed().as_secs_f32() / self.secs).clamp(0.0, 1.0);
        let lengths: Vec<f32> = self.points.windows(2).map(|w| w[0].distance(w[1])).collect();
        let total: f32 = lengths.iter().sum();
        if total <= f32::EPSILON {
            return (*self.points.last().unwrap(), true);
        }
        let mut left = t * total;
        for (i, len) in lengths.iter().enumerate() {
            if left <= *len {
                let a = self.points[i];
                let b = self.points[i + 1];
                return (a.lerp(b, if *len > 0.0 { left / len } else { 1.0 }), t >= 1.0);
            }
            left -= len;
        }
        (*self.points.last().unwrap(), true)
    }
}

/// Points to walk and how long (sim seconds) it should take, at the server's walking
/// speed. If we hear about the walk late, pick it up where the NPC should be by now
/// instead of rushing through the whole route in the time that is left.
fn walk_plan(here: Vec2, path: &[[i32; 2]], activity: &crate::net::NpcActivity, now: f32) -> (Vec<Vec2>, f32) {
    let cells: Vec<Vec2> = path.iter().map(|c| cell_center(IVec2::new(c[0], c[1]))).collect();
    let total = activity
        .pending_walk_seconds
        .filter(|s| *s > 0.0)
        .unwrap_or(path.len().saturating_sub(1) as f32 / SERVER_WALK_SPEED);
    let elapsed = activity.pending_started_at.map_or(0.0, |t| (now - t).clamp(0.0, total));
    let walked = ((cells.len() - 1) as f32 * elapsed / total.max(1e-3)).floor() as usize;
    let walked = walked.min(cells.len() - 1);
    let expected = cells[walked];
    // Close to where they should be: keep walking from here. Far behind: jump there.
    let start = if here.distance(expected) <= TILE * 3.0 { here } else { expected };
    let mut points = vec![start];
    points.extend(cells[walked + 1..].iter().copied());
    (points, total - elapsed)
}

#[derive(Component)]
pub struct NpcBubble {
    owner: Entity,
}

/// Small line under the name: what the NPC is currently trying to do.
#[derive(Component)]
pub struct NpcThought {
    owner: Entity,
}

#[derive(Resource, Default)]
pub struct NpcDirector {
    revision: Option<u64>,
}

pub fn sync_npcs(
    mut commands: Commands,
    world: Res<WorldSnapshot>,
    clock: Res<SimClock>,
    mut director: ResMut<NpcDirector>,
    mut avatars: Query<(Entity, &mut NpcAvatar, &Transform)>,
) {
    if director.revision == Some(world.revision) {
        return;
    }
    director.revision = Some(world.revision);

    let people: Vec<_> = world
        .world
        .persons
        .iter()
        .filter(|p| p.name != world.player.name && p.cell().is_some())
        .collect();

    // Gone from the world (or world reloaded): remove.
    for (entity, avatar, _) in &avatars {
        if !people.iter().any(|p| p.name == avatar.name) {
            commands.entity(entity).despawn();
        }
    }

    for person in people {
        let server_cell = person.cell().unwrap();
        let existing = avatars.iter_mut().find(|(_, a, _)| a.name == person.name);
        let Some((entity, mut avatar, transform)) = existing else {
            spawn_npc(&mut commands, &person.name, person.alive, person.hp, cell_center(server_cell));
            continue;
        };
        commands.entity(entity).insert(Interactive {
            kind: TargetKind::Person { name: person.name.clone() },
            prompt: format!("{}  (HP {})", person.name, person.hp),
            name: person.name.clone(),
            radius: TILE * 1.8,
        });
        let here = transform.translation.truncate();
        let activity = world.activity(&person.name);
        avatar.bubble = activity.map(action_text).unwrap_or_default();

        let path = activity.and_then(|a| a.pending_path.clone()).filter(|p| p.len() >= 2);
        match (activity, path) {
            (Some(a), Some(path)) => {
                let key = format!("{:?}{:?}", path.last(), a.pending_completes_at);
                if key == avatar.walk_key {
                    continue;
                }
                avatar.walk_key = key;
                let (points, sim_secs) = walk_plan(here, &path, a, clock.now());
                avatar.walk = Some(Walk {
                    points,
                    started: Instant::now(),
                    secs: (sim_secs * clock.wall_per_sim()).max(MIN_WALK_SECS),
                });
            }
            _ => {
                let target = cell_center(server_cell);
                if avatar.walk.is_some() || here.distance(target) < 2.0 {
                    continue;
                }
                avatar.walk_key.clear();
                avatar.walk = Some(Walk {
                    points: vec![here, target],
                    started: Instant::now(),
                    secs: (here.distance(target) / GLIDE_SPEED).max(MIN_WALK_SECS),
                });
            }
        }
    }
}

/// What the NPC is doing, for the bubble above its head.
fn action_text(activity: &NpcActivity) -> String {
    // The plan step in words, with how far through the plan they are.
    if !activity.step_line.is_empty() {
        return if activity.plan_len > 1 {
            format!("{} ({}/{})", activity.step_line, activity.plan_step, activity.plan_len)
        } else {
            activity.step_line.clone()
        };
    }
    let args = &activity.pending_args;
    let arg = |i: usize| args.get(i).cloned().unwrap_or_default();
    match activity.pending_action.as_str() {
        "" | "wait" | "noop" => String::new(),
        "move" => format!("-> {}", arg(2)),
        "talk" => format!("talking to {}", arg(1)),
        "pick_up" => format!("picking up {}", arg(1)),
        "give" => format!("giving {} to {}", arg(1), arg(2)),
        "attack" => format!("attacking {}!", arg(1)),
        "use_on" => format!("{} -> {}", arg(1), arg(2)),
        "interact" => format!("{} {}", arg(2).replace('_', " "), arg(1)),
        "drop" => format!("dropping {}", arg(1)),
        "use_item" => format!("using {}", arg(1)),
        other => other.replace('_', " "),
    }
}

fn spawn_npc(commands: &mut Commands, name: &str, alive: bool, hp: i32, at: Vec2) {
    let color = if alive {
        Color::srgb(0.78, 0.32, 0.43)
    } else {
        Color::srgb(0.28, 0.29, 0.31)
    };
    let npc = commands
        .spawn((
            Sprite {
                color,
                custom_size: Some(Vec2::splat(TILE * 0.85)),
                ..default()
            },
            Transform::from_xyz(at.x, at.y, 5.0),
            NpcAvatar {
                name: name.to_string(),
                walk: None,
                walk_key: String::new(),
                bubble: String::new(),
            },
            Interactive {
                kind: TargetKind::Person { name: name.to_string() },
                prompt: format!("{name}  (HP {hp})"),
                name: name.to_string(),
                radius: TILE * 1.8,
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
            Transform::from_xyz(0.0, -TILE * 0.75, 0.2),
        ));
        parent.spawn((
            Text2d::new(""),
            TextFont {
                font_size: 10.0,
                ..default()
            },
            TextColor(Color::srgb(0.66, 0.72, 0.80)),
            Transform::from_xyz(0.0, -TILE * 1.15, 0.2),
            NpcThought { owner: npc },
        ));
        parent.spawn((
            Text2d::new(""),
            TextFont {
                font_size: 13.0,
                ..default()
            },
            TextColor(Color::srgb(1.0, 0.92, 0.55)),
            Transform::from_xyz(0.0, TILE * 0.85, 0.3),
            NpcBubble { owner: npc },
        ));
    });
}

pub fn run_npc_behaviour(
    time: Res<Time>,
    speech: Res<SpeechPlayer>,
    mut avatars: Query<(&mut NpcAvatar, &mut Transform)>,
) {
    for (mut avatar, mut transform) in &mut avatars {
        if speech.in_conversation(&avatar.name) {
            // Stand still until the conversation has been said; the walk resumes where it was.
            if let Some(walk) = avatar.walk.as_mut() {
                walk.started += time.delta();
            }
            continue;
        }
        let Some(walk) = avatar.walk.as_ref() else { continue };
        let (pos, done) = walk.position();
        transform.translation.x = pos.x;
        transform.translation.y = pos.y;
        if done {
            avatar.walk = None;
        }
    }
}

pub fn update_npc_bubbles(
    world: Res<WorldSnapshot>,
    speech: Res<SpeechPlayer>,
    avatars: Query<&NpcAvatar>,
    mut bubbles: Query<(&NpcBubble, &mut Text2d), Without<NpcThought>>,
    mut thoughts: Query<(&NpcThought, &mut Text2d), Without<NpcBubble>>,
) {
    for (bubble, mut text) in &mut bubbles {
        let Ok(avatar) = avatars.get(bubble.owner) else {
            continue;
        };
        let state_label = world
            .activity(&avatar.name)
            .map(|a| a.state_label.clone())
            .unwrap_or_default();
        let next = if speech.is_speaking(&avatar.name) {
            String::new() // the speech bubble is up; don't stack the action text on it
        } else if !state_label.is_empty() && avatar.bubble.is_empty() {
            state_label
        } else {
            avatar.bubble.clone()
        };
        if text.0 != next {
            text.0 = next;
        }
    }
    for (thought, mut text) in &mut thoughts {
        let Ok(avatar) = avatars.get(thought.owner) else {
            continue;
        };
        let next = world
            .activity(&avatar.name)
            .map(thought_line)
            .unwrap_or_default();
        if text.0 != next {
            text.0 = next;
        }
    }
}

/// "goal -> target" for the line under an NPC's name.
pub fn thought_line(activity: &NpcActivity) -> String {
    let goal = match (activity.goal.as_str(), activity.target.as_str()) {
        ("", _) => return String::new(),
        (goal, "") => goal.to_string(),
        (goal, target) => format!("{goal} -> {target}"),
    };
    trim_line(&goal, 34)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_walk_heard_about_late_is_picked_up_mid_route_not_rushed() {
        let path: Vec<[i32; 2]> = (0..21).map(|x| [x, 0]).collect(); // 20 cells = 2 sim s
        let activity = crate::net::NpcActivity {
            pending_started_at: Some(10.0),
            pending_walk_seconds: Some(2.0),
            ..Default::default()
        };
        let start = cell_center(IVec2::new(0, 0));
        let (points, secs) = walk_plan(start, &path, &activity, 10.0);
        assert_eq!((points.len(), secs), (21, 2.0), "on time: the whole route, full time");
        let (points, secs) = walk_plan(start, &path, &activity, 11.0);
        assert_eq!(points[0], cell_center(IVec2::new(10, 0)), "late: jump to where they should be");
        assert_eq!((points.len(), secs), (11, 1.0), "...and walk the rest at the same speed");
    }
    use std::time::Duration;

    #[test]
    fn walk_follows_the_route_and_finishes() {
        let walk = Walk {
            points: vec![Vec2::ZERO, Vec2::new(10.0, 0.0), Vec2::new(10.0, 10.0)],
            started: Instant::now() - Duration::from_millis(500),
            secs: 1.0,
        };
        let (mid, done) = walk.position();
        assert!(!done && (mid - Vec2::new(10.0, 0.0)).length() < 1.0, "{mid:?}");
        let finished = Walk { started: Instant::now() - Duration::from_secs(2), ..walk };
        assert_eq!(finished.position(), (Vec2::new(10.0, 10.0), true));
    }
}
