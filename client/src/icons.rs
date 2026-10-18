//! Item icons: small pixel-art pictures drawn in code (no asset files), picked from an
//! item's kind and name, so a tree looks like a tree and a key like a key.

use bevy::asset::RenderAssetUsages;
use bevy::image::ImageSampler;
use bevy::prelude::*;
use bevy::render::render_resource::{Extent3d, TextureDimension, TextureFormat};
use std::collections::HashMap;

use crate::net::Thing;

const SIZE: usize = 12;

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum Icon {
    Tree,
    Berry,
    Fire,
    Wood,
    Coin,
    Stone,
    Door,
    Key,
    Herb,
    Potion,
    Document,
    Tool,
    Lantern,
    Tent,
    Stall,
    Board,
    Stove,
    Field,
    Dial,
    Crate,
    Fish,
    Water,
    Site,
}

const ALL: [Icon; 23] = [
    Icon::Tree, Icon::Berry, Icon::Fire, Icon::Wood, Icon::Coin, Icon::Stone, Icon::Door,
    Icon::Key, Icon::Herb, Icon::Potion, Icon::Document, Icon::Tool, Icon::Lantern, Icon::Tent,
    Icon::Stall, Icon::Board, Icon::Stove, Icon::Field, Icon::Dial, Icon::Crate, Icon::Fish, Icon::Water,
    Icon::Site,
];

/// Keyword → icon, checked in order (so "DoorKey" is a key and "StoneDial" a dial).
const RULES: &[(&[&str], Icon)] = &[
    (&["key"], Icon::Key),
    (&["site"], Icon::Site),
    (&["fishing_spot", "pool", "pond", "well"], Icon::Water),
    (&["fish"], Icon::Fish),
    (&["smoke"], Icon::Stove),
    (&["rod", "spear"], Icon::Tool),
    (&["door", "gate"], Icon::Door),
    (&["dial", "puzzle", "mechanism", "lever"], Icon::Dial),
    (&["tree", "woodlot", "forest", "oak", "pine"], Icon::Tree),
    (&["berry", "berries", "fruit", "apple"], Icon::Berry),
    (&["wood", "log", "plank", "stick"], Icon::Wood),
    (&["fire", "torch"], Icon::Fire),
    (&["coin", "token", "seal", "gold", "money"], Icon::Coin),
    (&["potion", "salve", "elixir", "consumable", "medicine"], Icon::Potion),
    (&["herb", "flower", "leaf", "plant"], Icon::Herb),
    (&["warrant", "plan", "document", "letter", "book", "scroll", "note", "map"], Icon::Document),
    (&["lantern", "lamp", "candle"], Icon::Lantern),
    (&["tent", "shelter", "bed"], Icon::Tent),
    (&["stall", "market", "shop"], Icon::Stall),
    (&["board", "sign", "notice"], Icon::Board),
    (&["stove", "oven", "kitchen"], Icon::Stove),
    (&["field", "wheat", "farm", "crop", "grain"], Icon::Field),
    (&["stone", "rock", "ore", "quarry"], Icon::Stone),
    (&["crowbar", "tool", "hammer", "axe", "pick", "knife"], Icon::Tool),
];

pub fn icon_for(thing: &Thing) -> Icon {
    let text = format!("{} {}", thing.name, thing.item_kind).to_lowercase();
    RULES
        .iter()
        .find(|(words, _)| words.iter().any(|w| text.contains(w)))
        .map(|(_, icon)| *icon)
        .unwrap_or(Icon::Crate)
}

fn pixels(icon: Icon) -> [&'static str; SIZE] {
    match icon {
        Icon::Tree => [
            "....GGGG....",
            "..GGggggGG..",
            ".GggggggggG.",
            "GgggggGggggG",
            "GggGgggggGgG",
            ".GggggGgggG.",
            "..GGggggGG..",
            "....GBBG....",
            ".....BB.....",
            ".....BB.....",
            "....BBBB....",
            "............",
        ],
        Icon::Berry => [
            "......gG....",
            ".....gG.....",
            "....Gg......",
            "..rrr.rrr...",
            ".rrwrrrwrr..",
            ".rrrrRrrrR..",
            ".RrrR.RrrR..",
            "..RR.rrr....",
            ".....rwrr...",
            ".....rrrR...",
            "......RR....",
            "............",
        ],
        Icon::Fire => [
            ".....o......",
            "....oo......",
            "....oyo..o..",
            "...oyyo.oo..",
            "..oyyyyooo..",
            "..oyywyyyo..",
            ".ooyywwyyoo.",
            ".oyywwwwyyo.",
            "..oyywwyyo..",
            ".BbBbbbbBbB.",
            "BbbBBbbBBbbB",
            "............",
        ],
        Icon::Wood => [
            "............",
            "............",
            "..BBBBBBBBt.",
            ".BbbbbbbbtTt",
            ".BbbbbbbbtTt",
            "..BBBBBBBBt.",
            ".BBBBBBBBt..",
            "BbbbbbbbtTt.",
            "BbbbbbbbtTt.",
            ".BBBBBBBBt..",
            "............",
            "............",
        ],
        Icon::Coin => [
            "............",
            "....oooo....",
            "..ooyyyyoo..",
            ".oyywwyyyyo.",
            ".oywyyyyyyo.",
            ".oyyyooyyyo.",
            ".oyyoyyyyyo.",
            ".oyyyooyyyo.",
            ".oyyyyyyyyo.",
            "..ooyyyyoo..",
            "....oooo....",
            "............",
        ],
        Icon::Stone => [
            "............",
            "............",
            "....SSSS....",
            "...SssswS...",
            "..SsswwssS..",
            ".SsssssssSS.",
            ".SssSsssssS.",
            "SsssssSssssS",
            "SssssssssSsS",
            "SSSSSSSSSSSS",
            "............",
            "............",
        ],
        Icon::Door => [
            "..BBBBBBBB..",
            ".BbbbbbbbbB.",
            ".BbBbbbBbbB.",
            ".BbBbbbBbbB.",
            ".BbBbbbBbbB.",
            ".BbbbbbbbbB.",
            ".BbBbbbByyB.",
            ".BbBbbbBbbB.",
            ".BbBbbbBbbB.",
            ".BbBbbbBbbB.",
            ".BbbbbbbbbB.",
            ".BBBBBBBBBB.",
        ],
        Icon::Key => [
            "............",
            "..yyyy......",
            ".yo..oy.....",
            ".y....y.....",
            ".y....y.....",
            ".yo..oyyyyyy",
            "..yyyy...y.y",
            ".........y.y",
            "............",
            "............",
            "............",
            "............",
        ],
        Icon::Herb => [
            "............",
            ".....g......",
            "....ggg..g..",
            "...gGgGgggg.",
            "..gggGggGg..",
            "...ggGgGg...",
            ".gggggGg....",
            "..GggGG.....",
            ".....G......",
            "....G.......",
            "...G........",
            "............",
        ],
        Icon::Potion => [
            "....tttt....",
            ".....ww.....",
            ".....ww.....",
            "....wwww....",
            "...wppppw...",
            "..wpppwppw..",
            "..wppppwpw..",
            "..wppppppw..",
            "..wPppppPw..",
            "...wPPPPw...",
            "....wwww....",
            "............",
        ],
        Icon::Document => [
            "..tttttttt..",
            "..twwwwwwt..",
            "..twSSSSwt..",
            "..twwwwwwt..",
            "..twSSSwwt..",
            "..twwwwwwt..",
            "..twSSSSwt..",
            "..twwwwwwt..",
            "..twSSwwwt..",
            "..twwwwrrt..",
            "..twwwwrrt..",
            "..tttttttt..",
        ],
        Icon::Tool => [
            "............",
            "..SSSSSS....",
            ".SsswsssS...",
            ".SSSSsSSS...",
            "....Bb......",
            "....Bb......",
            "....Bb......",
            "....Bb......",
            "....Bb......",
            "....Bb......",
            "....BB......",
            "............",
        ],
        Icon::Lantern => [
            ".....SS.....",
            "....S..S....",
            "...SSSSSS...",
            "...SyyyyS...",
            "...SywwyS...",
            "...SywwyS...",
            "...SyyyyS...",
            "...SSSSSS...",
            "....SSSS....",
            "............",
            "............",
            "............",
        ],
        Icon::Tent => [
            ".....B......",
            ".....o......",
            "....ooo.....",
            "...oooRo....",
            "...ooRRoo...",
            "..oooRRooo..",
            "..ooRkkRoo..",
            ".oooRkkRooo.",
            ".ooRRkkRRoo.",
            "oooRRkkRRooo",
            "............",
            "............",
        ],
        Icon::Stall => [
            "rwrwrwrwrwrw",
            "rwrwrwrwrwrw",
            ".rwrwrwrwrw.",
            ".B........B.",
            ".B........B.",
            ".B..o.y.r.B.",
            "BBBBBBBBBBBB",
            ".BbbbbbbbbB.",
            ".BbbbbbbbbB.",
            ".BBBBBBBBBB.",
            "............",
            "............",
        ],
        Icon::Board => [
            "BBBBBBBBBBBB",
            "BttttttttttB",
            "BtSSStSStttB",
            "BttttttttttB",
            "BtSStSSSSttB",
            "BttttttttttB",
            "BBBBBBBBBBBB",
            "..B......B..",
            "..B......B..",
            "..B......B..",
            "..B......B..",
            "............",
        ],
        Icon::Stove => [
            "..SS........",
            "..SS........",
            "SSSSSSSSSSSS",
            "SssssssssssS",
            "SsSSSSSSSSsS",
            "SsSkkkkkkSsS",
            "SsSkoyyokSsS",
            "SsSkooookSsS",
            "SsSSSSSSSSsS",
            "SssssssssssS",
            "SSSSSSSSSSSS",
            ".S........S.",
        ],
        Icon::Field => [
            "..y...y...y.",
            ".yty.yty.yty",
            "..y...y...y.",
            "..g...g...g.",
            ".gg..gg..gg.",
            "..g...g...g.",
            "..g...g...g.",
            "bbbbbbbbbbbb",
            "BbBbBbBbBbBb",
            "bbbbbbbbbbbb",
            "............",
            "............",
        ],
        Icon::Dial => [
            "....SSSS....",
            "..SSssssSS..",
            ".SssuSSussS.",
            ".SssssSsssS.",
            "SsussSSssusS",
            "SssSSkkSSssS",
            "SssSSkkSSssS",
            "SsussssssusS",
            ".SsssSSsssS.",
            ".SssuSSussS.",
            "..SSssssSS..",
            "....SSSS....",
        ],
        Icon::Fish => [
            "............",
            "............",
            "............",
            "..uuuu....u.",
            ".uwwuuu..uu.",
            "uuwkuuuuuuu.",
            "uuuuuuuuuuu.",
            ".uucuuu..uu.",
            "..uuuu....u.",
            "............",
            "............",
            "............",
        ],
        Icon::Water => [
            "............",
            "............",
            "...uuuuuu...",
            ".uuuccuuuuu.",
            "uuuuuuuccuuu",
            "uuccuuuuuuuu",
            "uuuuuucuuuuu",
            ".uuuuuuuucu.",
            "...uuuuuu...",
            "............",
            "............",
            "............",
        ],
        Icon::Site => [
            "............",
            ".B........B.",
            ".BtttttttttB",
            ".B.t....t..B",
            ".B..t..t...B",
            ".B...tt....B",
            ".B..t..t...B",
            ".B.t....t..B",
            ".BtttttttttB",
            ".B........B.",
            "sSsSsSsSsSsS",
            "............",
        ],
        Icon::Crate => [
            "............",
            ".BBBBBBBBBB.",
            ".BbbbBBbbbB.",
            ".BbbbBBbbbB.",
            ".BBBBBBBBBB.",
            ".BbbbbbbbbB.",
            ".BbbbyybbbB.",
            ".BbbbbbbbbB.",
            ".BbbbbbbbbB.",
            ".BBBBBBBBBB.",
            "............",
            "............",
        ],
    }
}

fn palette(c: char) -> Option<[u8; 4]> {
    Some(match c {
        '.' => return None,
        'g' => [86, 170, 74, 255],
        'G' => [38, 104, 48, 255],
        'b' => [150, 98, 52, 255],
        'B' => [92, 58, 30, 255],
        't' => [214, 184, 132, 255],
        'T' => [176, 140, 88, 255],
        'r' => [214, 52, 58, 255],
        'R' => [140, 26, 40, 255],
        'y' => [250, 214, 70, 255],
        'o' => [236, 132, 36, 255],
        's' => [168, 172, 176, 255],
        'S' => [96, 100, 108, 255],
        'w' => [244, 244, 236, 255],
        'k' => [28, 28, 32, 255],
        'u' => [72, 140, 220, 255],
        'p' => [170, 84, 214, 255],
        'P' => [110, 46, 150, 255],
        'c' => [150, 210, 240, 255],
        _ => [255, 0, 255, 255],
    })
}

fn image_for(icon: Icon) -> Image {
    image_from_rows(&pixels(icon))
}

/// A 12x12 picture from rows of palette letters ('.' = transparent).
pub fn image_from_rows(rows: &[&str; SIZE]) -> Image {
    let mut data = Vec::with_capacity(SIZE * SIZE * 4);
    for row in rows {
        for c in row.chars() {
            data.extend_from_slice(&palette(c).unwrap_or([0, 0, 0, 0]));
        }
    }
    let mut image = Image::new(
        Extent3d { width: SIZE as u32, height: SIZE as u32, depth_or_array_layers: 1 },
        TextureDimension::D2,
        data,
        TextureFormat::Rgba8UnormSrgb,
        RenderAssetUsages::RENDER_WORLD,
    );
    image.sampler = ImageSampler::nearest(); // crisp pixels when scaled up
    image
}

#[derive(Resource, Default)]
pub struct IconAtlas {
    images: HashMap<Icon, Handle<Image>>,
}

impl IconAtlas {
    pub fn get(&self, icon: Icon) -> Handle<Image> {
        self.images.get(&icon).cloned().unwrap_or_default()
    }
}

pub fn setup_icons(mut commands: Commands, mut images: ResMut<Assets<Image>>) {
    let images = ALL.iter().map(|&icon| (icon, images.add(image_for(icon)))).collect();
    commands.insert_resource(IconAtlas { images });
}

/// Bevy's built-in font has no Chinese glyphs: swap a system CJK font in as the default
/// font (every text uses it), so typed Chinese and Chinese replies show up.
pub fn use_cjk_font(mut fonts: ResMut<Assets<Font>>) {
    const CANDIDATES: [&str; 3] = ["C:/Windows/Fonts/Deng.ttf", "C:/Windows/Fonts/simhei.ttf", "/System/Library/Fonts/PingFang.ttc"];
    for path in CANDIDATES {
        let Ok(bytes) = std::fs::read(path) else { continue };
        if let Ok(font) = Font::try_from_bytes(bytes) {
            let _ = fonts.insert(&Handle::<Font>::default(), font);
            return;
        }
    }
}

// ---------------------------------------------------------------------------
// People: a little pixel figure; skin, hair and clothes picked from the name
// ---------------------------------------------------------------------------

/// h hair · s skin · k eyes / shoes · c shirt · p trousers · b satchel (traders)
const PERSON: [&str; SIZE] = [
    "....hhhh....",
    "...hhhhhh...",
    "...hssssh...",
    "...skssks...",
    "....ssss....",
    "..cccccccc..",
    ".sccccccccs.",
    ".sccccccccs.",
    "...pppppp...",
    "...pp..pp...",
    "...pp..pp...",
    "..kkk..kkk..",
];
/// Traders carry a satchel on the hip.
const SATCHEL: [(usize, usize); 4] = [(6, 8), (6, 9), (7, 8), (7, 9)];

const SKINS: [[u8; 3]; 4] = [[241, 194, 155], [224, 172, 125], [198, 134, 92], [141, 85, 54]];
const HAIRS: [[u8; 3]; 6] = [[40, 30, 25], [110, 70, 35], [205, 165, 85], [150, 55, 30], [125, 125, 130], [30, 30, 45]];
const SHIRTS: [[u8; 3]; 8] = [
    [196, 62, 62], [70, 150, 80], [210, 150, 50], [140, 80, 170],
    [60, 140, 150], [190, 100, 140], [120, 110, 70], [200, 200, 190],
];
const TROUSERS: [[u8; 3]; 3] = [[60, 60, 90], [85, 62, 40], [55, 72, 52]];
const PLAYER_SHIRT: [u8; 3] = [64, 184, 255];

fn name_hash(name: &str) -> u32 {
    name.bytes().fold(2166136261u32, |h, b| (h ^ b as u32).wrapping_mul(16777619))
}

/// The figure for one person (same name -> same look).
pub fn person_image(name: &str, trader: bool, player: bool) -> Image {
    let h = name_hash(name);
    let skin = SKINS[(h % 4) as usize];
    let hair = HAIRS[((h >> 3) % 6) as usize];
    let shirt = if player { PLAYER_SHIRT } else { SHIRTS[((h >> 7) % 8) as usize] };
    let trousers = TROUSERS[((h >> 11) % 3) as usize];
    let mut data = Vec::with_capacity(SIZE * SIZE * 4);
    for (y, row) in PERSON.iter().enumerate() {
        for (x, c) in row.chars().enumerate() {
            let c = if trader && SATCHEL.contains(&(y, x)) { 'b' } else { c };
            let rgb = match c {
                'h' => Some(hair),
                's' => Some(skin),
                'k' => Some([25, 22, 28]),
                'c' => Some(shirt),
                'p' => Some(trousers),
                'b' => Some([120, 78, 40]),
                _ => None,
            };
            data.extend_from_slice(&rgb.map_or([0, 0, 0, 0], |[r, g, b]| [r, g, b, 255]));
        }
    }
    let mut image = Image::new(
        Extent3d { width: SIZE as u32, height: SIZE as u32, depth_or_array_layers: 1 },
        TextureDimension::D2,
        data,
        TextureFormat::Rgba8UnormSrgb,
        RenderAssetUsages::RENDER_WORLD,
    );
    image.sampler = ImageSampler::nearest();
    image
}

/// One figure per person, made the first time they are drawn.
#[derive(Resource, Default)]
pub struct PersonIcons {
    images: HashMap<(String, bool, bool), Handle<Image>>,
}

impl PersonIcons {
    pub fn get(&mut self, name: &str, trader: bool, player: bool, images: &mut Assets<Image>) -> Handle<Image> {
        self.images
            .entry((name.to_string(), trader, player))
            .or_insert_with(|| images.add(person_image(name, trader, player)))
            .clone()
    }
}

/// Fallen people are drawn grey.
pub fn person_tint(alive: bool) -> Color {
    if alive { Color::WHITE } else { Color::srgb(0.35, 0.35, 0.38) }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn thing(name: &str, kind: &str) -> Thing {
        Thing { name: name.into(), item_kind: kind.into(), ..Default::default() }
    }

    #[test]
    fn people_look_different_but_always_the_same() {
        let a = person_image("Ash", false, false);
        let b = person_image("Sage", false, false);
        assert_eq!(a.data, person_image("Ash", false, false).data, "same name, same look");
        assert_ne!(a.data, b.data, "different people look different");
        assert_ne!(a.data, person_image("Ash", true, false).data, "traders carry a satchel");
        assert!(PERSON.iter().all(|r| r.chars().count() == SIZE));
    }

    #[test]
    fn every_icon_is_a_full_grid_of_known_colours() {
        for icon in ALL {
            for row in pixels(icon) {
                assert_eq!(row.chars().count(), SIZE, "{icon:?}: {row}");
                assert!(row.chars().all(|c| c == '.' || palette(c) != Some([255, 0, 255, 255])), "{icon:?}: {row}");
            }
        }
    }

    #[test]
    fn things_get_the_icon_their_name_suggests() {
        assert_eq!(icon_for(&thing("DoorKey", "key")), Icon::Key);
        assert_eq!(icon_for(&thing("VaultDoor", "door")), Icon::Door);
        assert_eq!(icon_for(&thing("StoneDial", "puzzle")), Icon::Dial);
        assert_eq!(icon_for(&thing("OakTree", "tree")), Icon::Tree);
        assert_eq!(icon_for(&thing("Woodlot", "source")), Icon::Tree);
        assert_eq!(icon_for(&thing("Wood1", "")), Icon::Wood);
        assert_eq!(icon_for(&thing("Campfire", "")), Icon::Fire);
        assert_eq!(icon_for(&thing("HealingSalve", "consumable")), Icon::Potion);
        assert_eq!(icon_for(&thing("RedHerb", "herb")), Icon::Herb);
        assert_eq!(icon_for(&thing("Mystery", "")), Icon::Crate);
    }
}
