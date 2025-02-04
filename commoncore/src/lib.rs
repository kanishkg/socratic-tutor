use pyo3::prelude::*;
use pyo3::exceptions::PyValueError;
use pyo3::wrap_pyfunction;
use std::collections::HashMap;
use std::sync::Arc;

pub mod domain;
use crate::domain::Domain;
use crate::domain::equations::Equations;
use crate::domain::fractions::Fractions;
use crate::domain::ternary::TernaryAddition;
use crate::domain::multiplication::Multiplication;
use crate::domain::sorting::Sorting;
use crate::domain::rubiks_cube::RubiksCube;
use crate::domain::key_to_door::KeyToDoor;

extern crate num_rational;
extern crate pest;
#[macro_use]
extern crate pest_derive;

thread_local!{
    pub static DOMAINS: HashMap<&'static str, Arc<dyn Domain>> = {
        let mut map : HashMap<&'static str, Arc<dyn Domain>>  = HashMap::new();
        map.insert("equations-ct", Arc::new(Equations {}));
        map.insert("fractions", Arc::new(Fractions::new(4, 4)));
        map.insert("ternary-addition", Arc::new(TernaryAddition::new(15)));
        map.insert("ternary-addition-small", Arc::new(TernaryAddition::new(8)));
        map.insert("multiplication", Arc::new(Multiplication {}));
        map.insert("sorting", Arc::new(Sorting::new(12)));

        map.insert("key-to-door", Arc::new(KeyToDoor::new(5, 0.1)));
        map.insert("rubiks-cube-20", Arc::new(RubiksCube::new(20)));
        map.insert("rubiks-cube-50", Arc::new(RubiksCube::new(50)));
        map
    };
}

/// Generates a problem in the specified domain with the given seed.
#[pyfunction]
fn generate(domain: String, seed: u64) -> PyResult<String> {
    DOMAINS.with(|domains| {
        if let Some(d) = domains.get(domain.as_str()) {
            let s = d.generate(seed);
            Ok(s)
        } else {
            Err(PyValueError::new_err(format!("Invalid domain.")))
        }
    })
}

/// Returns the actions and rewards for each given state.
#[pyfunction]
fn step(domain: String, states: Vec<String>) -> PyResult<Vec<Option<Vec<(String, String, String)>>>> {
    DOMAINS.with(|domains| {
        if let Some(d) = domains.get(domain.as_str()) {
            let mut result = Vec::with_capacity(states.len());
            for s in states.iter() {
                result.push(d.step(s.clone()).map(|v| v.iter().map(|a| (a.next_state.clone(),
                                                                        a.formal_description.clone(),
                                                                        a.human_description.clone())).collect()));
            }
            Ok(result)
        } else {
            Err(PyValueError::new_err(format!("Invalid domain.")))
        }
    })
}

/// A Python module implemented in Rust.
#[pymodule]
fn commoncore(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(generate, m)?)?;
    m.add_function(wrap_pyfunction!(step, m)?)?;

    Ok(())
}
