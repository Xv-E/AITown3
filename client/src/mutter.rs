//! Mutters: NPCs talk to themselves on the road. The server sends recent mutters
//! (`simulation.mutters`, each with an id); each new one shows above the NPC for a little
//! while: a mood icon, and the line (if any) in the head text. Unlike conversations,
//! nobody stops walking for a mutter.

use bevy::prelude::*;
use std::collections::{HashMap, HashSet};
use std::time::Instant;

use crate::adapter::NpcAvatar;
use crate::net::{Mutter, WorldSnapshot};
use crate::speech::line_secs;

/// An icon-only mutter stays up this long (seconds).
const ICON_SECS: f32 = 2.5;

const MOODS: [(&str, [&str; 12]); 10] = [
    ("happy", [
        "....yyyy....", "..yyyyyyyy..", ".yyyyyyyyyy.",
        ".yyyyyyyyyy.", "yyykyyyykyyy", "yyyyyyyyyyyy",
        "yyyyyyyyyyyy", "yykyyyyyykyy", ".yykkkkkkyy.",
        ".yyyyyyyyyy.", "..yyyyyyyy..", "....yyyy....",
    ]),
    ("angry", [
        "....oooo....", "..oooooooo..", ".oooooooooo.",
        ".okkooookkoo", "oookooookooo", "oooooooooooo",
        "oooooooooooo", "oookkkkkkooo", ".okooooooko.",
        ".oooooooooo.", "..oooooooo..", "....oooo....",
    ]),
    ("sad", [
        "....yyyy....", "..yyyyyyyy..", ".yyyyyyyyyy.",
        ".yyyyyyyyyy.", "yyykyyyykyyy", "yyyuyyyyyyyy",
        "yyyuyyyyyyyy", "yyyyyyyyyyyy", ".yykkkkkkyy.",
        ".ykyyyyyyky.", "..yyyyyyyy..", "....yyyy....",
    ]),
    ("scared", [
        "....cccc....", "..cccccccc..", ".cccccccccc.",
        ".ccccccccccu", "cckkcccckkcu", "cckkcccckkcc",
        "cccccccccccc", "cccckkkkcccc", ".ccckcckccc.",
        ".cccccccccc.", "..cccccccc..", "....cccc....",
    ]),
    ("tired", [
        "....yyyy....", "..yyyyyyyy..", ".yyyyyyyyyy.",
        ".yyyyyyyyyy.", "yykkkyykkkyy", "yyyyyyyyyyyy",
        "yyyyyyyyyyyy", "yyyyykkyyyyy", ".yyyyyyyyyy.",
        ".yyyyyyyyyy.", "..yyyyyyyy..", "....yyyy....",
    ]),
    ("thinking", [
        "............", "............", "............",
        "............", "............", "............",
        "............", ".ww..ww..ww.", ".ww..ww..ww.",
        "............", "............", "............",
    ]),
    ("love", [
        "............", ".rrr...rrr..", "rrrrr.rrrrr.",
        "rrrrrrrrrrr.", "rrrrrrrrrrr.", "rrrrrrrrrrr.",
        ".rrrrrrrrr..", "..rrrrrrr...", "...rrrrr....",
        "....rrr.....", ".....r......", "............",
    ]),
    ("surprised", [
        "....yyyy....", "....yyyy....", "....yyyy....",
        "....yyyy....", "....yyyy....", "....yyyy....",
        "....yyyy....", "....yyyy....", "............",
        "....yyyy....", "....yyyy....", "............",
    ]),
    ("hurt", [
        "....yyyy....", "..yywwwwyy..", ".yywrrrrwyy.",
        ".yywwwwwwyy.", "yyykyyyykyyy", "yyyyyyyyyyyy",
        "yyyyyyyyyyyy", "yyyykkkkyyyy", ".yyyyyyyyyy.",
        ".yyyyyyyyyy.", "..yyyyyyyy..", "....yyyy....",
    ]),
    ("secret", [
        "....yyyy....", "..yyyyyyyy..", ".yyyyyyyyyy.",
        ".yyyyyyyyyy.", "yykkyyyykkyy", "yyyyyyyyyyyy",
        "yyyyyyrryyyy", "yyyykrrkyyyy", ".yyyyyrryyy.",
        ".yyyyyyyyyy.", "..yyyyyyyy..", "....yyyy....",
    ]),
];

#[derive(Resource, Default)]
pub struct MoodIcons {
    images: HashMap<&'static str, Handle<Image>>,
}

pub fn setup_mood_icons(mut commands: Commands, mut images: ResMut<Assets<Image>>) {
    let images = MOODS
        .iter()
        .map(|(key, rows)| (*key, images.add(crate::icons::image_from_rows(rows))))
        .collect();
    commands.insert_resource(MoodIcons { images });
}

struct Shown {
    mutter: Mutter,
    since: Instant,
}

impl Shown {
    fn secs(&self) -> f32 {
        if self.mutter.text.trim().is_empty() {
            ICON_SECS
        } else {
            line_secs(&self.mutter.text)
        }
    }
}

/// What each NPC is muttering right now (the newest one wins).
#[derive(Resource, Default)]
pub struct MutterBoard {
    seen: HashSet<String>,
    synced_once: bool,
    revision: Option<u64>,
    shown: HashMap<String, Shown>,
}

impl MutterBoard {
    pub fn current(&self, who: &str) -> Option<&Mutter> {
        self.shown.get(who).map(|s| &s.mutter)
    }

    /// Show mutters not seen before. On the first sync after connecting, the ones already
    /// there are old news: mark them seen without showing them.
    fn take_new(&mut self, world: &WorldSnapshot) {
        for m in &world.simulation.mutters {
            if !self.seen.insert(m.id.clone()) || !self.synced_once {
                continue;
            }
            self.shown.insert(m.who.clone(), Shown { mutter: m.clone(), since: Instant::now() });
        }
        if world.online {
            self.synced_once = true;
        }
    }
}

pub fn update_mutters(world: Res<WorldSnapshot>, mut board: ResMut<MutterBoard>) {
    if board.revision != Some(world.revision) {
        board.revision = Some(world.revision);
        board.take_new(&world);
    }
    board.shown.retain(|_, s| s.since.elapsed().as_secs_f32() < s.secs());
}

/// Mood icon above an NPC's head (hidden when there is nothing to show).
#[derive(Component)]
pub struct NpcMood {
    pub owner: Entity,
}

pub fn render_mood_icons(
    board: Res<MutterBoard>,
    icons: Res<MoodIcons>,
    avatars: Query<&NpcAvatar>,
    mut moods: Query<(&NpcMood, &mut Sprite, &mut Visibility)>,
) {
    for (mood, mut sprite, mut visibility) in &mut moods {
        let icon = avatars
            .get(mood.owner)
            .ok()
            .and_then(|a| board.current(a.name()))
            .and_then(|m| icons.images.get(m.icon.as_str()));
        match icon {
            Some(image) => {
                if sprite.image != *image {
                    sprite.image = image.clone();
                }
                visibility.set_if_neq(Visibility::Inherited);
            }
            None => {
                visibility.set_if_neq(Visibility::Hidden);
            }
        }
    }
}
