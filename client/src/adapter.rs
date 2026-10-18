//! NPCs in the seamless world.
//!
//! The server decides what each NPC does and, for anything that needs walking, sends the
//! route (`pending_path`, world cells) and when the action completes. Here every NPC lives
//! in the one world scene and follows that route so it arrives when the server's walk
//! ends, then shows what it is doing above its head. Without a route it glides to the
//! cell the server says it stands on.

use bevy::prelude::*;
use std::time::Instant;

use crate::mutter::{MutterBoard, NpcMood};
use crate::net::{trim_line, NpcActivity, SimClock, WorldSnapshot};
use crate::speech::SpeechPlayer;
use crate::scene::{cell_center, Interactive, TargetKind, TILE};

/// Server walking speed (world cells per sim second); must match spatial.WALK_CELLS_PER_SEC.
const SERVER_WALK_SPEED: f32 = 10.0;
/// Head text: what the NPC is doing, a mutter, or something important slipping out.
const BUBBLE_COLOR: Color = Color::srgb(1.0, 0.92, 0.55);
const MUTTER_COLOR: Color = Color::srgb(0.84, 0.86, 0.92);
const SECRET_COLOR: Color = Color::srgb(1.0, 0.66, 0.30);
/// Fallback glide speed when correcting to the server position (pixels per second).
const GLIDE_SPEED: f32 = 6.0 * TILE;
const MIN_WALK_SECS: f32 = 0.2;
/// An NPC behind schedule walks at most this many times faster to catch up (never teleports).
const CATCH_UP: f32 = 2.0;
/// Further than this from where they should be (cells) is a jump, not a walk (e.g. world reload).
const TELEPORT_TILES: f32 = 25.0;

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
    // Behind: hurry along from where they are (up to CATCH_UP x walking speed) instead of
    // jumping. Only something absurd (a world reload) is worth a jump.
    let start = if here.distance(expected) <= TILE * TELEPORT_TILES { here } else { expected };
    let mut points = vec![start];
    points.extend(cells[walked + 1..].iter().copied());
    let length: f32 = points.windows(2).map(|w| w[0].distance(w[1])).sum::<f32>() / TILE;
    let hurried = length / (SERVER_WALK_SPEED * CATCH_UP);
    (points, (total - elapsed).max(hurried))
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

#[allow(clippy::too_many_arguments)]
pub fn sync_npcs(
    mut commands: Commands,
    world: Res<WorldSnapshot>,
    clock: Res<SimClock>,
    mut director: ResMut<NpcDirector>,
    mut avatars: Query<(Entity, &mut NpcAvatar, &Transform)>,
    mut sprites: Query<&mut Sprite, With<NpcAvatar>>,
    mut looks: ResMut<crate::icons::PersonIcons>,
    mut images: ResMut<Assets<Image>>,
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
        let trader = world.character(&person.name).is_some_and(|c| c.trader);
        let Some((entity, mut avatar, transform)) = existing else {
            let look = looks.get(&person.name, trader, false, &mut images);
            spawn_npc(&mut commands, &person.name, person.alive, person.hp, cell_center(server_cell), look);
            continue;
        };
        if let Ok(mut sprite) = sprites.get_mut(entity) {
            let tint = crate::icons::person_tint(person.alive);
            if sprite.color != tint {
                sprite.color = tint;
            }
        }
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

fn spawn_npc(commands: &mut Commands, name: &str, alive: bool, hp: i32, at: Vec2, look: Handle<Image>) {
    let npc = commands
        .spawn((
            Sprite {
                image: look,
                color: crate::icons::person_tint(alive),
                custom_size: Some(Vec2::splat(TILE * 1.0)),
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
            TextColor(BUBBLE_COLOR),
            Transform::from_xyz(0.0, TILE * 0.85, 0.3),
            NpcBubble { owner: npc },
        ));
        parent.spawn((
            Sprite { custom_size: Some(Vec2::splat(TILE * 0.7)), ..default() },
            Transform::from_xyz(0.0, TILE * 1.45, 0.4),
            Visibility::Hidden,
            NpcMood { owner: npc },
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
    mutters: Res<MutterBoard>,
    avatars: Query<&NpcAvatar>,
    mut bubbles: Query<(&NpcBubble, &mut Text2d, &mut TextColor), Without<NpcThought>>,
    mut thoughts: Query<(&NpcThought, &mut Text2d), Without<NpcBubble>>,
) {
    for (bubble, mut text, mut color) in &mut bubbles {
        let Ok(avatar) = avatars.get(bubble.owner) else {
            continue;
        };
        let state_label = world
            .activity(&avatar.name)
            .map(|a| a.state_label.clone())
            .unwrap_or_default();
        let mutter = mutters.current(&avatar.name).filter(|m| !m.text.trim().is_empty());
        let (next, tint) = if speech.is_speaking(&avatar.name) {
            (String::new(), BUBBLE_COLOR) // the speech bubble is up; don't stack the action text on it
        } else if let Some(m) = mutter {
            // Something important the player was close enough to catch stands out.
            if m.important && m.overheard {
                (format!("! {} !", trim_line(&m.text, 44)), SECRET_COLOR)
            } else {
                (format!("~ {} ~", trim_line(&m.text, 44)), MUTTER_COLOR)
            }
        } else if !state_label.is_empty() && avatar.bubble.is_empty() {
            (state_label, BUBBLE_COLOR)
        } else {
            (avatar.bubble.clone(), BUBBLE_COLOR)
        };
        if text.0 != next {
            text.0 = next;
        }
        if color.0 != tint {
            color.0 = tint;
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
    // A posted request shows first: "! needs stone (3c)".
    if !activity.request.is_empty() {
        return trim_line(&format!("! {}", activity.request), 34);
    }
    if activity.thinking_hard {
        return "(thinking it over...)".to_string();
    }
    // The server keeps the last goal after the plan is done; only show it while they're working on it.
    let working = !activity.pending_action.is_empty()
        || matches!(activity.status.as_str(), "acting" | "ready");
    if !working {
        return String::new();
    }
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
    fn the_goal_line_clears_once_they_are_done() {
        let mut a = crate::net::NpcActivity {
            goal: "Go to location".into(),
            target: "Tavern".into(),
            status: "completed".into(),
            ..Default::default()
        };
        assert_eq!(thought_line(&a), "", "arrived: nothing to show");
        a.status = "acting".into();
        assert_eq!(thought_line(&a), "Go to location -> Tavern");
        a.status = "waiting".into();
        a.pending_action = "move".into();
        assert_eq!(thought_line(&a), "Go to location -> Tavern", "still on the way");
    }

    #[test]
    fn a_late_walk_hurries_along_instead_of_teleporting() {
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
        assert_eq!(points[0], start, "late: no jump, they hurry from where they are");
        assert_eq!(points.len(), 11, "...along the rest of the route");
        assert!((secs - 1.0).abs() < 1e-3, "20 cells at double speed = the 1 s that is left: {secs}");
        let far = cell_center(IVec2::new(0, 40));
        let (points, _) = walk_plan(far, &path, &activity, 11.0);
        assert_eq!(points[0], cell_center(IVec2::new(10, 0)), "absurdly far off (a reload): jump");
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
