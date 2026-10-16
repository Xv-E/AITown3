//! Speech bubbles: conversations play line by line above the speakers' heads.
//!
//! The server sends recent conversations (`simulation.conversations`, whole exchanges
//! with an id). Each new one is queued and played back: one line at a time, held long
//! enough to read, in a bubble that follows whoever is speaking.

use bevy::{prelude::*, text::{TextBounds, TextLayoutInfo}};
use std::collections::{HashMap, HashSet};
use std::time::Instant;

use crate::adapter::NpcAvatar;
use crate::net::{ConversationLine, WorldSnapshot};
use crate::scene::{PlayerAvatar, TILE};

const BUBBLE_WIDTH: f32 = 230.0;
const BUBBLE_PAD: Vec2 = Vec2::new(10.0, 6.0);
const BUBBLE_Z: f32 = 30.0;
const GAP_SECS: f32 = 0.25;

/// How long a line stays up: long enough to read, never forever.
pub fn line_secs(text: &str) -> f32 {
    (1.2 + text.chars().count() as f32 * 0.055).clamp(2.0, 6.0)
}

struct Playback {
    participants: [String; 2],
    lines: Vec<ConversationLine>,
    index: usize,
    line_started: Instant,
}

impl Playback {
    fn current(&self) -> Option<&ConversationLine> {
        self.lines.get(self.index)
    }
}

#[derive(Resource, Default)]
pub struct SpeechPlayer {
    seen: HashSet<String>,
    synced_once: bool,
    revision: Option<u64>,
    playing: Vec<Playback>,
}

impl SpeechPlayer {
    /// Who is saying what right now.
    pub fn speaking(&self) -> HashMap<&str, &str> {
        self.playing
            .iter()
            .filter_map(Playback::current)
            .map(|l| (l.speaker.as_str(), l.text.as_str()))
            .collect()
    }

    /// Taking part in a conversation that is still being played (speaking or listening):
    /// they stand where they are until it is over.
    pub fn in_conversation(&self, name: &str) -> bool {
        self.playing.iter().any(|p| {
            p.participants.iter().any(|n| n == name) || p.lines.iter().any(|l| l.speaker == name)
        })
    }

    pub fn is_speaking(&self, name: &str) -> bool {
        self.playing.iter().filter_map(Playback::current).any(|l| l.speaker == name)
    }

    /// Queue conversations not seen before. On the first sync after connecting, the ones
    /// already there are old news: mark them seen without replaying them.
    fn take_new(&mut self, world: &WorldSnapshot) {
        for convo in &world.simulation.conversations {
            if !self.seen.insert(convo.id.clone()) || !self.synced_once {
                continue;
            }
            let lines: Vec<ConversationLine> =
                convo.lines.iter().filter(|l| !l.text.trim().is_empty()).cloned().collect();
            if !lines.is_empty() {
                self.playing.push(Playback {
                    participants: [convo.a.clone(), convo.b.clone()],
                    lines,
                    index: 0,
                    line_started: Instant::now(),
                });
            }
        }
        if world.online {
            self.synced_once = true;
        }
    }

    fn advance(&mut self) {
        for p in &mut self.playing {
            if let Some(line) = p.current() {
                if p.line_started.elapsed().as_secs_f32() >= line_secs(&line.text) + GAP_SECS {
                    p.index += 1;
                    p.line_started = Instant::now();
                }
            }
        }
        self.playing.retain(|p| p.current().is_some());
    }
}

pub fn update_speech(world: Res<WorldSnapshot>, mut speech: ResMut<SpeechPlayer>) {
    if speech.revision != Some(world.revision) {
        speech.revision = Some(world.revision);
        speech.take_new(&world);
    }
    speech.advance();
}

#[derive(Component)]
pub struct SpeechBubble {
    speaker: String,
}

#[derive(Component)]
pub struct SpeechText;

#[derive(Component)]
pub struct SpeechBackground;

/// One bubble per speaker: spawned when they start talking, kept above their head,
/// removed when they stop.
pub fn render_speech_bubbles(
    mut commands: Commands,
    speech: Res<SpeechPlayer>,
    world: Res<WorldSnapshot>,
    npcs: Query<(&NpcAvatar, &Transform), Without<SpeechBubble>>,
    player: Query<&Transform, (With<PlayerAvatar>, Without<SpeechBubble>)>,
    mut bubbles: Query<(Entity, &SpeechBubble, &mut Transform, &Children)>,
    mut texts: Query<(&mut Text2d, &TextLayoutInfo), With<SpeechText>>,
    mut backs: Query<&mut Sprite, With<SpeechBackground>>,
) {
    let speaking = speech.speaking();
    let head = |name: &str| -> Option<Vec2> {
        if name == world.player.name {
            return player.single().ok().map(|t| t.translation.truncate());
        }
        npcs.iter()
            .find(|(a, _)| a.name() == name)
            .map(|(_, t)| t.translation.truncate())
    };

    let mut shown: HashSet<String> = HashSet::new();
    for (entity, bubble, mut transform, children) in &mut bubbles {
        let (Some(text), Some(pos)) = (speaking.get(bubble.speaker.as_str()), head(&bubble.speaker)) else {
            commands.entity(entity).despawn();
            continue;
        };
        shown.insert(bubble.speaker.clone());
        transform.translation = (pos + Vec2::new(0.0, TILE * 1.6)).extend(BUBBLE_Z);
        let mut size = Vec2::ZERO;
        for child in children.iter() {
            if let Ok((mut t, layout)) = texts.get_mut(child) {
                if t.0 != *text {
                    t.0 = text.to_string();
                }
                let scale = if layout.scale_factor > 0.0 { layout.scale_factor } else { 1.0 };
                size = layout.size / scale;
            }
        }
        for child in children.iter() {
            if let Ok(mut sprite) = backs.get_mut(child) {
                sprite.custom_size = Some(size + BUBBLE_PAD * 2.0);
                // Hidden until the text has been laid out once.
                sprite.color = if size == Vec2::ZERO { Color::NONE } else { Color::srgba(0.05, 0.06, 0.08, 0.88) };
            }
        }
    }

    for (speaker, text) in speaking {
        if shown.contains(speaker) {
            continue;
        }
        let Some(pos) = head(speaker) else { continue };
        commands
            .spawn((
                Transform::from_translation((pos + Vec2::new(0.0, TILE * 1.6)).extend(BUBBLE_Z)),
                Visibility::default(),
                SpeechBubble { speaker: speaker.to_string() },
            ))
            .with_children(|b| {
                b.spawn((
                    Sprite { color: Color::NONE, custom_size: Some(Vec2::ZERO), ..default() },
                    Transform::from_xyz(0.0, 0.0, 0.0),
                    SpeechBackground,
                ));
                b.spawn((
                    Text2d::new(text),
                    TextFont { font_size: 13.0, ..default() },
                    TextColor(Color::srgb(0.97, 0.97, 0.94)),
                    TextLayout::new_with_justify(Justify::Center),
                    TextBounds { width: Some(BUBBLE_WIDTH), height: None },
                    Transform::from_xyz(0.0, 0.0, 0.1),
                    SpeechText,
                ));
            });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::net::{Conversation, WorldSnapshot};

    fn world_with(ids: &[&str]) -> WorldSnapshot {
        let mut world = WorldSnapshot::offline();
        world.online = true;
        world.simulation.conversations = ids
            .iter()
            .map(|id| Conversation {
                id: id.to_string(),
                a: "Ash".into(),
                b: "Sage".into(),
                lines: vec![
                    ConversationLine { speaker: "Ash".into(), text: "Did you take the berries?".into() },
                    ConversationLine { speaker: "Sage".into(), text: "Only the ones nobody wanted.".into() },
                ],
            })
            .collect();
        world
    }

    #[test]
    fn old_conversations_are_not_replayed_new_ones_are() {
        let mut speech = SpeechPlayer::default();
        speech.take_new(&world_with(&["1"]));
        assert!(speech.playing.is_empty(), "already there when we connected");
        speech.take_new(&world_with(&["1", "2"]));
        assert_eq!(speech.playing.len(), 1);
        assert_eq!(speech.speaking().get("Ash"), Some(&"Did you take the berries?"));
        assert!(!speech.is_speaking("Sage"), "one line at a time");
        assert!(speech.in_conversation("Sage"), "the listener waits too");
        assert!(!speech.in_conversation("Reed"));
    }

    #[test]
    fn lines_alternate_then_finish() {
        let mut speech = SpeechPlayer::default();
        speech.take_new(&world_with(&[]));
        speech.take_new(&world_with(&["7"]));
        let p = &mut speech.playing[0];
        p.line_started = Instant::now() - std::time::Duration::from_secs(10);
        speech.advance();
        assert!(speech.is_speaking("Sage") && !speech.is_speaking("Ash"));
        speech.playing[0].line_started = Instant::now() - std::time::Duration::from_secs(10);
        speech.advance();
        assert!(speech.playing.is_empty());
    }

    #[test]
    fn reading_time_grows_with_length_within_limits() {
        assert_eq!(line_secs("Hi."), 2.0);
        assert!(line_secs(&"word ".repeat(12)) > 3.0);
        assert_eq!(line_secs(&"x".repeat(500)), 6.0);
    }
}
