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
}

const ALL: [Icon; 20] = [
    Icon::Tree, Icon::Berry, Icon::Fire, Icon::Wood, Icon::Coin, Icon::Stone, Icon::Door,
    Icon::Key, Icon::Herb, Icon::Potion, Icon::Document, Icon::Tool, Icon::Lantern, Icon::Tent,
    Icon::Stall, Icon::Board, Icon::Stove, Icon::Field, Icon::Dial, Icon::Crate,
];

/// Keyword → icon, checked in order (so "DoorKey" is a key and "StoneDial" a dial).
const RULES: &[(&[&str], Icon)] = &[
    (&["key"], Icon::Key),
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
        _ => [255, 0, 255, 255],
    })
}

fn image_for(icon: Icon) -> Image {
    let mut data = Vec::with_capacity(SIZE * SIZE * 4);
    for row in pixels(icon) {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn thing(name: &str, kind: &str) -> Thing {
        Thing { name: name.into(), item_kind: kind.into(), ..Default::default() }
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
